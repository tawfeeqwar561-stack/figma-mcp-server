"""
Planner: converts a natural-language UI description into a DesignPlan.
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

_SYSTEM_PROMPT = """You are a UI design planner. Given a natural-language description of a screen, output a single JSON object using EXACTLY this shape. Do not add extra fields. Do not nest groups. Keep elements as a FLAT list, all as children of one root frame.

{
  "screen_name": "string",
  "color_styles": [],
  "text_styles": [],
  "variables": [],
  "elements": [
    {
      "type": "frame",
      "name": "string",
      "x": 0, "y": 0, "width": 375, "height": 812,
      "content": "", "font_size": 16, "corner_radius": 0,
      "color": {"r": 1.0, "g": 1.0, "b": 1.0},
      "children": [
        {"type": "text", "name": "", "x": 0, "y": 0, "width": 100, "height": 30, "content": "label text", "font_size": 16, "corner_radius": 0, "color": {"r": 0, "g": 0, "b": 0}, "children": []},
        {"type": "rectangle", "name": "", "x": 0, "y": 0, "width": 100, "height": 40, "content": "", "font_size": 16, "corner_radius": 8, "color": {"r": 0.9, "g": 0.9, "b": 0.9}, "children": []}
      ]
    }
  ]
}

Rules:
- Color values are floats between 0.0 and 1.0, never 0-255.
- Only use "type": "frame", "text", or "rectangle". Nothing else.
- Every element must have ALL fields shown above, in that order, no extras.
- children can be an empty list [] for text and rectangle elements.
- Do not use "group" or "component_set" types.
- Output ONLY the JSON object. No explanation, no markdown."""


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
    """Fix a common small-model mistake: {"r":.., "g":.., "g":..} instead of "r","g","b"."""
    def fix_color_obj(match: "re.Match") -> str:
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


def parse_plan_json(raw_text: str) -> DesignPlan:
    attempts = [
        raw_text,
        _strip_code_fences(raw_text),
        _extract_json_object(_strip_code_fences(raw_text)),
        _basic_json_repairs(_extract_json_object(_strip_code_fences(raw_text))),
        _fix_duplicate_color_keys(_basic_json_repairs(_extract_json_object(_strip_code_fences(raw_text)))),
    ]
    last_error = None
    for attempt_text in attempts:
        try:
            parsed = json.loads(attempt_text)
            return DesignPlan(**parsed)
        except (json.JSONDecodeError, ValidationError) as exc:
            last_error = exc
            continue
    logger.error("All JSON repair attempts failed. Raw output: %s", raw_text[:800])
    raise ValueError(f"Could not parse LLM output into a valid DesignPlan: {last_error}")


class OllamaPlanner(Planner):
    def __init__(self, base_url: str, model: str, timeout: float = 300.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout

    async def generate_plan(self, prompt: str) -> DesignPlan:
        body = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "format": "json",
            "stream": False,
            "keep_alive": "10m",
            "options": {
                "temperature": 0.1,
                "num_predict": 1500,
            },
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(f"{self._base_url}/api/chat", json=body)
            response.raise_for_status()
        data = response.json()
        raw_text = data.get("message", {}).get("content", "")
        if not raw_text:
            raise ValueError("Ollama returned an empty response.")
        return parse_plan_json(raw_text)

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
        return parse_plan_json(raw_text)


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
                "Primary planner (%s) failed: %s. Falling back to %s.",
                type(self._primary).__name__, exc, type(self._fallback).__name__,
            )
            return await self._fallback.generate_plan(prompt)


def get_planner() -> Planner:
    ollama = OllamaPlanner(
        base_url=config.OLLAMA_BASE_URL,
        model=config.OLLAMA_MODEL,
    )
    return FallbackPlanner(primary=ollama, fallback=TemplatePlanner())
