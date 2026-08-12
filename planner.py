"""
Planner: converts a natural-language UI description into a DesignPlan.
Includes retry logic and a semantic quality gate so garbled or
placeholder-leaking output triggers another attempt instead of being
accepted or immediately falling back to the template planner.
"""

import json
import logging
import re
from abc import ABC, abstractmethod

import httpx
from pydantic import ValidationError

import config
from design_plan import DesignPlan

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You are a UI design planner for a mobile app screen. Read the user's request carefully, then invent screen_name, element names, and text content that are 100% original and specific to THEIR request. Never reuse any word from this instruction block itself as actual output content.

Output a single JSON object with this exact shape (only the KEYS and structure below are fixed; every VALUE must be written fresh by you based on the user's request):

{
  "screen_name": <a short title describing the user's requested screen>,
  "color_styles": [],
  "text_styles": [],
  "variables": [],
  "elements": [
    {
      "type": "frame",
      "name": <a short internal name for this frame>,
      "x": 0, "y": 0, "width": 375, "height": 812,
      "content": "", "font_size": 16, "corner_radius": 0,
      "color": {"r": <0.0-1.0>, "g": <0.0-1.0>, "b": <0.0-1.0>},
      "children": [
        { "type": "text" or "rectangle", "name": <short internal name>, "x": <int>, "y": <int>, "width": <int>, "height": <int>, "content": <real text for "text" type, empty string for "rectangle" type>, "font_size": <int>, "corner_radius": <int>, "color": {"r": <0.0-1.0>, "g": <0.0-1.0>, "b": <0.0-1.0>}, "children": [] }
      ]
    }
  ]
}

RULES:
- One root "frame", 375 wide by 812 tall, unless the user implies otherwise.
- Add one child element (text or rectangle) per distinct thing the user mentioned, positioned top-to-bottom without overlapping (increase y for each one).
- "text" elements need real, request-specific content (an actual label, name, price, etc. — never blank, never generic).
- "rectangle" elements always have content: "" (empty) — they are visual containers, buttons, or input boxes, not text.
- color_styles, text_styles, variables: always empty arrays [].
- Colors are floats 0.0 to 1.0, never 0-255.
- Only "frame" elements may have non-empty children; text/rectangle children are always [].
- Output ONLY the JSON object. No explanation, no markdown, no commentary."""

_PLACEHOLDER_MARKERS = {"string", "label text", ""}
_MAX_RETRIES = 3


class Planner(ABC):
    @abstractmethod
    async def generate_plan(self, prompt: str) -> DesignPlan: ...


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _extract_json_object(text: str) -> str:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return text
    return text[start:end + 1]


def _basic_json_repairs(text: str) -> str:
    return re.sub(r",\s*([}\]])", r"\1", text)


def _fix_duplicate_color_keys(text: str) -> str:
    def fix_color_obj(match):
        inner = match.group(1)
        pairs = re.findall(r'"(\w)"\s*:\s*([\d.]+)', inner)
        seen = {}
        for k, v in pairs:
            seen[k] = v
        r = seen.get("r", "0")
        g = seen.get("g", "0")
        b = seen.get("b", "0")
        return f'{{"r": {r}, "g": {g}, "b": {b}}}'

    return re.sub(r'\{("[rgb]"\s*:\s*[\d.]+(?:\s*,\s*"[rgb]"\s*:\s*[\d.]+)*)\}', fix_color_obj, text)


def _strip_invalid_token_arrays(text: str) -> str:
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return text
    if isinstance(obj, dict):
        for key in ("color_styles", "text_styles", "variables"):
            if key in obj:
                obj[key] = []
    return json.dumps(obj)


def _flatten_deep_children(text: str) -> str:
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return text

    def fix_node(node):
        if not isinstance(node, dict):
            return node
        children = node.get("children", [])
        if node.get("type") != "frame" and children:
            node["children"] = []
        else:
            node["children"] = [fix_node(c) for c in children]
        return node

    if isinstance(obj, dict) and "elements" in obj:
        obj["elements"] = [fix_node(e) for e in obj["elements"]]
    return json.dumps(obj)


def parse_plan_json(raw_text: str) -> DesignPlan:
    base_attempts = [
        raw_text,
        _strip_code_fences(raw_text),
        _extract_json_object(_strip_code_fences(raw_text)),
        _basic_json_repairs(_extract_json_object(_strip_code_fences(raw_text))),
        _fix_duplicate_color_keys(_basic_json_repairs(_extract_json_object(_strip_code_fences(raw_text)))),
    ]

    all_attempts = list(base_attempts)
    for attempt in base_attempts:
        repaired = _strip_invalid_token_arrays(attempt)
        repaired = _flatten_deep_children(repaired)
        all_attempts.append(repaired)

    last_error = None
    for attempt_text in all_attempts:
        try:
            parsed = json.loads(attempt_text)
            return DesignPlan(**parsed)
        except (json.JSONDecodeError, ValidationError) as exc:
            last_error = exc
            continue

    logger.error("All JSON repair attempts failed. Raw output: %s", raw_text[:800])
    raise ValueError(f"Could not parse LLM output into a valid DesignPlan: {last_error}")


def _check_semantic_quality(plan: DesignPlan) -> None:
    """
    Raise if the plan is technically valid JSON but semantically useless —
    e.g. the model copied placeholder text from the prompt's examples
    instead of generating content relevant to the request.
    """
    if plan.screen_name.strip().lower() in _PLACEHOLDER_MARKERS:
        raise ValueError(f"Placeholder leakage in screen_name: {plan.screen_name!r}")

    def walk(nodes):
        for node in nodes:
            if node.content.strip().lower() in _PLACEHOLDER_MARKERS and node.type == "text":
                raise ValueError(f"Placeholder leakage in text content: {node.content!r}")
            if node.children:
                walk(node.children)

    walk(plan.elements)


class OllamaPlanner(Planner):
    def __init__(self, base_url: str, model: str, timeout: float = 300.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout

    async def _call_once(self, prompt: str, nudge: bool) -> DesignPlan:
        user_content = prompt
        if nudge:
            user_content = (
                f"{prompt}\n\n(Reminder: use real, specific values relevant to this "
                f"request — do not reuse the word 'string' or 'label text' anywhere "
                f"in your answer.)"
            )
        body = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            "format": "json",
            "stream": False,
            "keep_alive": "10m",
            "options": {"temperature": 0.4, "num_predict": 1800},
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(f"{self._base_url}/api/chat", json=body)
            response.raise_for_status()
        data = response.json()
        raw_text = data.get("message", {}).get("content", "")
        if not raw_text:
            raise ValueError("Ollama returned an empty response.")
        plan = parse_plan_json(raw_text)
        _check_semantic_quality(plan)
        return plan

    async def generate_plan(self, prompt: str) -> DesignPlan:
        last_error: Exception | None = None
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                plan = await self._call_once(prompt, nudge=(attempt > 1))
                if attempt > 1:
                    logger.info("OllamaPlanner succeeded on retry attempt %d", attempt)
                return plan
            except Exception as exc:
                last_error = exc
                logger.warning("OllamaPlanner attempt %d/%d failed: %s", attempt, _MAX_RETRIES, exc)
        raise last_error  # type: ignore[misc]

    async def is_available(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.get(f"{self._base_url}/api/tags")
                return resp.status_code == 200
        except (httpx.ConnectError, httpx.TimeoutException):
            return False


class AnthropicPlanner(Planner):
    _API_URL = "https://api.anthropic.com/v1/messages"
    _MODEL = "claude-sonnet-4-5"

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    async def generate_plan(self, prompt: str) -> DesignPlan:
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        body = {
            "model": self._MODEL,
            "max_tokens": 4000,
            "system": _SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": prompt}],
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(self._API_URL, headers=headers, json=body)
            response.raise_for_status()
        data = response.json()
        raw_text = "".join(block.get("text", "") for block in data.get("content", []))
        plan = parse_plan_json(raw_text)
        _check_semantic_quality(plan)
        return plan


class TemplatePlanner(Planner):
    async def generate_plan(self, prompt: str) -> DesignPlan:
        p = prompt.lower()
        if "login" in p or "sign in" in p or "log in" in p:
            return _login_template()
        if "dashboard" in p:
            return _dashboard_template()
        raise ValueError(
            f"TemplatePlanner has no template matching prompt: {prompt!r}. "
            "Supported keywords: 'login'/'sign in', 'dashboard'."
        )


def _login_template() -> DesignPlan:
    return DesignPlan.model_validate({
        "screen_name": "Login Screen",
        "elements": [{
            "type": "frame", "name": "Login Screen", "x": 0, "y": 0,
            "width": 375, "height": 812,
            "color": {"r": 1, "g": 1, "b": 1},
            "children": [
                {"type": "text", "x": 40, "y": 100, "content": "Welcome Back", "font_size": 28},
                {"type": "rectangle", "x": 40, "y": 200, "width": 295, "height": 50,
                 "corner_radius": 8, "color": {"r": 0.95, "g": 0.95, "b": 0.95}},
                {"type": "text", "x": 55, "y": 215, "content": "Email", "font_size": 14},
                {"type": "rectangle", "x": 40, "y": 270, "width": 295, "height": 50,
                 "corner_radius": 8, "color": {"r": 0.95, "g": 0.95, "b": 0.95}},
                {"type": "text", "x": 55, "y": 285, "content": "Password", "font_size": 14},
                {"type": "rectangle", "x": 40, "y": 350, "width": 295, "height": 50,
                 "corner_radius": 25, "color": {"r": 0.2, "g": 0.4, "b": 1.0}},
                {"type": "text", "x": 150, "y": 365, "content": "Log In",
                 "font_size": 16, "color": {"r": 1, "g": 1, "b": 1}},
            ],
        }],
    })


def _dashboard_template() -> DesignPlan:
    return DesignPlan.model_validate({
        "screen_name": "Dashboard",
        "elements": [{
            "type": "frame", "name": "Dashboard", "x": 0, "y": 0,
            "width": 800, "height": 600,
            "color": {"r": 0.97, "g": 0.97, "b": 0.98},
            "children": [
                {"type": "text", "x": 40, "y": 30, "content": "Dashboard", "font_size": 24},
                {"type": "rectangle", "x": 40, "y": 90, "width": 220, "height": 120,
                 "corner_radius": 12, "color": {"r": 1, "g": 1, "b": 1}},
                {"type": "text", "x": 60, "y": 110, "content": "Revenue", "font_size": 14},
                {"type": "rectangle", "x": 280, "y": 90, "width": 220, "height": 120,
                 "corner_radius": 12, "color": {"r": 1, "g": 1, "b": 1}},
                {"type": "text", "x": 300, "y": 110, "content": "Users", "font_size": 14},
                {"type": "image_placeholder", "x": 40, "y": 240, "width": 460, "height": 260,
                 "content": "Chart"},
            ],
        }],
    })


class FallbackPlanner(Planner):
    def __init__(self, primary: Planner, fallback: Planner) -> None:
        self._primary = primary
        self._fallback = fallback

    async def generate_plan(self, prompt: str) -> DesignPlan:
        try:
            return await self._primary.generate_plan(prompt)
        except Exception as exc:
            logger.warning(
                "Primary planner (%s) failed after retries: %s. Falling back to %s.",
                type(self._primary).__name__, exc, type(self._fallback).__name__,
            )
            return await self._fallback.generate_plan(prompt)


def get_planner() -> Planner:
    ollama = OllamaPlanner(
        base_url=config.OLLAMA_BASE_URL,
        model=config.OLLAMA_MODEL,
    )
    return FallbackPlanner(primary=ollama, fallback=TemplatePlanner())

