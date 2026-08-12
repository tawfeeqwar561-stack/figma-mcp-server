"""
Planner: converts a natural-language UI description into a DesignPlan.

Key design decision: the LLM is asked ONLY for content and element type
(heading, text, input, button) — never for x/y/width/height/color. All
positioning is computed deterministically in Python (see build_design_plan),
which makes layout guaranteed non-overlapping regardless of model quality,
and shrinks what the model must get right, which improves speed and
reliability and reduces timeout risk.
"""

import json
import logging
import re
from abc import ABC, abstractmethod
from typing import Literal

import httpx
from pydantic import BaseModel, Field, ValidationError

import config
from design_plan import DesignPlan, DesignNode, ColorRGB

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3

# ---------------------------------------------------------------------------
# Simplified schema the LLM actually fills in
# ---------------------------------------------------------------------------

class SimpleElement(BaseModel):
    type: Literal["heading", "text", "input", "button"]
    content: str


class SimplePlan(BaseModel):
    screen_name: str
    elements: list[SimpleElement] = Field(min_length=1)


_SYSTEM_PROMPT = """You are a UI content planner. Read the user's screen request and output a JSON object listing the screen name and the elements it needs, in top-to-bottom order.

Output EXACTLY this shape, nothing else:
{
  "screen_name": "<short title for this screen, specific to the request>",
  "elements": [
    {"type": "heading" | "text" | "input" | "button", "content": "<real, specific text for this element>"}
  ]
}

Element type meanings:
- "heading": a large title at the top of the screen.
- "text": a small label or line of information.
- "input": a labeled field the user types into (e.g. email, password, search). Use the field's label as content, e.g. "Email".
- "button": a tappable action. Use the button's label as content, e.g. "Submit".

RULES:
- Every "content" value must be specific to the user's request. Never leave it blank, never write "string" or "text" or "label".
- Include one element per distinct thing the user mentioned.
- Do not include x, y, width, height, or color — only type and content.
- Output ONLY the JSON object. No explanation, no markdown."""


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


def _parse_simple_plan(raw_text: str) -> SimplePlan:
    attempts = [
        raw_text,
        _strip_code_fences(raw_text),
        _extract_json_object(_strip_code_fences(raw_text)),
        _basic_json_repairs(_extract_json_object(_strip_code_fences(raw_text))),
    ]
    last_error = None
    for attempt_text in attempts:
        try:
            parsed = json.loads(attempt_text)
            return SimplePlan(**parsed)
        except (json.JSONDecodeError, ValidationError) as exc:
            last_error = exc
            continue
    logger.error("Could not parse simple plan. Raw output: %s", raw_text[:500])
    raise ValueError(f"Could not parse LLM output into a SimplePlan: {last_error}")


_PLACEHOLDER_MARKERS = {"string", "text", "label", ""}


def _check_semantic_quality(plan: SimplePlan) -> None:
    if plan.screen_name.strip().lower() in _PLACEHOLDER_MARKERS:
        raise ValueError(f"Placeholder leakage in screen_name: {plan.screen_name!r}")
    for el in plan.elements:
        if el.content.strip().lower() in _PLACEHOLDER_MARKERS:
            raise ValueError(f"Placeholder leakage in element content: {el.content!r}")


# ---------------------------------------------------------------------------
# Deterministic layout engine — this is what guarantees no overlap
# ---------------------------------------------------------------------------

_FRAME_WIDTH = 375
_MARGIN = 30
_CONTENT_WIDTH = _FRAME_WIDTH - (2 * _MARGIN)


def build_design_plan(simple: SimplePlan) -> DesignPlan:
    """
    Deterministically lay out elements top-to-bottom. The model never
    chooses coordinates — this function always produces valid, non
    -overlapping positions regardless of how many elements there are.
    """
    y = 60
    children: list[DesignNode] = []

    for el in simple.elements:
        if el.type == "heading":
            children.append(DesignNode(
                type="text", name="heading", x=_MARGIN, y=y,
                width=_CONTENT_WIDTH, height=36, content=el.content,
                font_size=24, color=ColorRGB(r=0.1, g=0.1, b=0.1),
            ))
            y += 36 + 24

        elif el.type == "text":
            children.append(DesignNode(
                type="text", name="text_line", x=_MARGIN, y=y,
                width=_CONTENT_WIDTH, height=22, content=el.content,
                font_size=15, color=ColorRGB(r=0.25, g=0.25, b=0.25),
            ))
            y += 22 + 16

        elif el.type == "input":
            children.append(DesignNode(
                type="text", name="input_label", x=_MARGIN, y=y,
                width=_CONTENT_WIDTH, height=18, content=el.content,
                font_size=13, color=ColorRGB(r=0.4, g=0.4, b=0.4),
            ))
            y += 18 + 6
            children.append(DesignNode(
                type="rectangle", name="input_field", x=_MARGIN, y=y,
                width=_CONTENT_WIDTH, height=46, content="",
                corner_radius=8, color=ColorRGB(r=0.93, g=0.93, b=0.93),
            ))
            y += 46 + 24

        elif el.type == "button":
            btn_height = 50
            children.append(DesignNode(
                type="rectangle", name="button_bg", x=_MARGIN, y=y,
                width=_CONTENT_WIDTH, height=btn_height, content="",
                corner_radius=25, color=ColorRGB(r=0.2, g=0.45, b=0.95),
            ))
            children.append(DesignNode(
                type="text", name="button_label",
                x=_MARGIN, y=y + (btn_height // 2) - 10,
                width=_CONTENT_WIDTH, height=20, content=el.content,
                font_size=16, color=ColorRGB(r=1.0, g=1.0, b=1.0),
            ))
            y += btn_height + 24

    frame_height = max(812, y + 60)
    frame = DesignNode(
        type="frame",
        name=re.sub(r"[^a-zA-Z0-9]+", "_", simple.screen_name.strip().lower()) or "screen",
        x=0, y=0, width=_FRAME_WIDTH, height=frame_height,
        color=ColorRGB(r=1.0, g=1.0, b=1.0),
        children=children,
    )
    return DesignPlan(screen_name=simple.screen_name, elements=[frame])


# ---------------------------------------------------------------------------
# Planner interface + backends
# ---------------------------------------------------------------------------

class Planner(ABC):
    @abstractmethod
    async def generate_plan(self, prompt: str) -> DesignPlan: ...


class OllamaPlanner(Planner):
    def __init__(self, base_url: str, model: str, timeout: float = 90.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout

    async def _call_once(self, prompt: str, nudge: bool) -> SimplePlan:
        user_content = prompt
        if nudge:
            user_content = f"{prompt}\n\n(Reminder: every content value must be specific real text, never the word 'string', 'text', or 'label'.)"
        body = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            "format": "json",
            "stream": False,
            "keep_alive": "10m",
            "options": {"temperature": 0.4, "num_predict": 500},
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(f"{self._base_url}/api/chat", json=body)
            response.raise_for_status()
        data = response.json()
        raw_text = data.get("message", {}).get("content", "")
        if not raw_text:
            raise ValueError("Ollama returned an empty response.")
        simple = _parse_simple_plan(raw_text)
        _check_semantic_quality(simple)
        return simple

    async def generate_plan(self, prompt: str) -> DesignPlan:
        last_error: Exception | None = None
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                simple = await self._call_once(prompt, nudge=(attempt > 1))
                if attempt > 1:
                    logger.info("OllamaPlanner succeeded on retry attempt %d", attempt)
                return build_design_plan(simple)
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
            "max_tokens": 1000,
            "system": _SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": prompt}],
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(self._API_URL, headers=headers, json=body)
            response.raise_for_status()
        data = response.json()
        raw_text = "".join(block.get("text", "") for block in data.get("content", []))
        simple = _parse_simple_plan(raw_text)
        _check_semantic_quality(simple)
        return build_design_plan(simple)


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
    simple = SimplePlan(screen_name="Login Screen", elements=[
        SimpleElement(type="heading", content="Welcome Back"),
        SimpleElement(type="input", content="Email"),
        SimpleElement(type="input", content="Password"),
        SimpleElement(type="button", content="Log In"),
    ])
    return build_design_plan(simple)


def _dashboard_template() -> DesignPlan:
    simple = SimplePlan(screen_name="Dashboard", elements=[
        SimpleElement(type="heading", content="Dashboard"),
        SimpleElement(type="text", content="Revenue: $12,400"),
        SimpleElement(type="text", content="Active Users: 342"),
        SimpleElement(type="button", content="View Report"),
    ])
    return build_design_plan(simple)


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
