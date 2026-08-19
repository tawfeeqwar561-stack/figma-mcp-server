"""
Planner: converts a natural-language UI description into a DesignPlan.

Key design decisions:
- The LLM is asked ONLY for content, element type, and a theme choice —
  never for x/y/width/height/color. All positioning and styling is
  computed deterministically in Python, guaranteeing non-overlapping,
  visually consistent layouts regardless of model quality.
- A small curated set of design token palettes replaces ad-hoc colors,
  giving every generated screen a coherent, professional look instead
  of one flat default blue/gray.
"""

import json
import logging
import re
from abc import ABC, abstractmethod
from typing import Literal

import httpx
from pydantic import BaseModel, Field, ValidationError

import config
from design_plan import DesignPlan, DesignNode, ColorRGB, EffectConfig

logger = logging.getLogger(__name__)

_MAX_RETRIES = 2

# ---------------------------------------------------------------------------
# Design token palettes — curated, not model-generated (reliability by design)
# ---------------------------------------------------------------------------

class Palette:
    def __init__(self, bg, heading, text, label, input_bg, button_bg, button_text,
                 shadow_opacity_soft=0.06, shadow_opacity_strong=0.14):
        self.bg = bg
        self.heading = heading
        self.text = text
        self.label = label
        self.input_bg = input_bg
        self.button_bg = button_bg
        self.button_text = button_text
        self.shadow_opacity_soft = shadow_opacity_soft
        self.shadow_opacity_strong = shadow_opacity_strong


_THEMES: dict[str, Palette] = {
    "professional_blue": Palette(
        bg=(1.0, 1.0, 1.0), heading=(0.07, 0.09, 0.15), text=(0.35, 0.38, 0.45),
        label=(0.5, 0.53, 0.58), input_bg=(0.95, 0.96, 0.98),
        button_bg=(0.15, 0.4, 0.95), button_text=(1.0, 1.0, 1.0),
    ),
    "midnight_premium": Palette(
        bg=(0.07, 0.08, 0.10), heading=(0.97, 0.97, 0.98), text=(0.75, 0.76, 0.8),
        label=(0.55, 0.56, 0.6), input_bg=(0.15, 0.16, 0.19),
        button_bg=(0.35, 0.65, 1.0), button_text=(0.05, 0.06, 0.08),
        shadow_opacity_soft=0.25, shadow_opacity_strong=0.4,
    ),
    "calm_wellness": Palette(
        bg=(0.98, 0.97, 0.95), heading=(0.13, 0.24, 0.18), text=(0.35, 0.42, 0.38),
        label=(0.55, 0.6, 0.56), input_bg=(0.93, 0.95, 0.92),
        button_bg=(0.35, 0.55, 0.45), button_text=(1.0, 1.0, 1.0),
    ),
    "warm_friendly": Palette(
        bg=(0.99, 0.97, 0.93), heading=(0.28, 0.17, 0.08), text=(0.45, 0.36, 0.28),
        label=(0.6, 0.52, 0.45), input_bg=(0.96, 0.92, 0.86),
        button_bg=(0.93, 0.5, 0.2), button_text=(1.0, 1.0, 1.0),
    ),
}
_DEFAULT_THEME = "professional_blue"


def _rgb(t):
    return ColorRGB(r=t[0], g=t[1], b=t[2])


def _soft_shadow(palette):
    return EffectConfig(type="DROP_SHADOW", color=ColorRGB(r=0, g=0, b=0),
                         radius=8, offset_x=0, offset_y=2, opacity=palette.shadow_opacity_soft)


def _strong_shadow(palette):
    return EffectConfig(type="DROP_SHADOW", color=ColorRGB(r=0, g=0, b=0),
                         radius=12, offset_x=0, offset_y=4, opacity=palette.shadow_opacity_strong)


# ---------------------------------------------------------------------------
# Simplified schema the LLM actually fills in
# ---------------------------------------------------------------------------

class SimpleElement(BaseModel):
    type: Literal["heading", "text", "input", "button"]
    # Tighter than DesignNode.content since this is pre-layout-expansion
    # raw model output -- see H-6 in bridge-security-hardening.
    content: str = Field(max_length=500)


class SimplePlan(BaseModel):
    screen_name: str
    theme: str = _DEFAULT_THEME
    # Turns the "cap at 10 elements" prompt instruction into a real,
    # enforced ceiling -- see H-6 in bridge-security-hardening.
    elements: list[SimpleElement] = Field(min_length=1, max_length=10)


_THEME_NAMES = ", ".join(f'"{k}"' for k in _THEMES)

_SYSTEM_PROMPT = f"""You are a UI content planner. Read the user's screen request and output a JSON object listing the screen name, a theme, and the elements it needs, in top-to-bottom order.

Output EXACTLY this shape, nothing else:
{{
  "screen_name": "<short title for this screen, specific to the request>",
  "theme": "<one of: {_THEME_NAMES}>",
  "elements": [
    {{"type": "heading" | "text" | "input" | "button", "content": "<real, specific text for this element>"}}
  ]
}}

Theme selection guide:
- "professional_blue": default, use for business, finance, productivity, general apps.
- "midnight_premium": use for premium, luxury, dark-mode-requested, or "sleek/modern" requests.
- "calm_wellness": use for health, wellness, meditation, fitness, therapy-related requests.
- "warm_friendly": use for social, community, food, casual, or friendly-toned requests.

Element type meanings and how to map common UI concepts to them:
- "heading": a large title. Use for: page titles, welcome messages, section titles.
- "text": a line of information or description. Use for: "balance card" -> a text line showing an amount like "Balance: $2,450.00". "spending insights" -> a text summary line. "recent transactions" -> 2-3 separate text elements, one per transaction, e.g. "Coffee Shop -$4.50". Any descriptive or informational content becomes "text".
- "input": a labeled field the user types into. Use for: email, password, search, name, amount fields.
- "button": a tappable action. Use for: "quick actions" -> one button per action mentioned (or 2-3 generic ones like "Send", "Request" if unspecified). "bottom navigation" -> 2-4 buttons, one per nav item (e.g. "Home", "Cards", "Settings").

RULES:
- Every "content" value must be specific and realistic (invent plausible real values like amounts, names, dates where the request implies data). NEVER output an empty string for content.
- Never leave content blank, never write "string" or "text" or "label".
- Cap the total number of elements at 10, even if the request could imply more.
- Ignore purely stylistic/tone instructions beyond picking the theme (e.g. "clean typography") — they do not map to elements.
- Do not include x, y, width, height, or color — only type, content, and theme.
- Output ONLY the JSON object. No explanation, no markdown."""


def _strip_code_fences(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _extract_json_object(text):
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return text
    return text[start:end + 1]


def _basic_json_repairs(text):
    return re.sub(r",\s*([}\]])", r"\1", text)


def _parse_simple_plan(raw_text):
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


def _check_semantic_quality(plan):
    if plan.screen_name.strip().lower() in _PLACEHOLDER_MARKERS:
        raise ValueError(f"Placeholder leakage in screen_name: {plan.screen_name!r}")
    for el in plan.elements:
        if el.content.strip().lower() in _PLACEHOLDER_MARKERS:
            raise ValueError(f"Placeholder leakage in element content: {el.content!r}")


# ---------------------------------------------------------------------------
# Deterministic layout engine — guarantees no overlap, applies design tokens
# ---------------------------------------------------------------------------

_FRAME_WIDTH = 375
_MARGIN = 30
_CONTENT_WIDTH = _FRAME_WIDTH - (2 * _MARGIN)


def _estimate_wrapped_lines(content, width, font_size):
    """
    Rough estimate of how many lines `content` will wrap into at `width`,
    given `font_size`. Figma text wrapping depends on font metrics we
    don't have access to here, so this uses an approximation: average
    character width is roughly 0.55x the font size for typical UI fonts.
    Good enough to reserve safe vertical space, not pixel-perfect.
    """
    explicit_lines = content.split("\n")
    total_lines = 0
    avg_char_width = font_size * 0.55
    chars_per_line = max(1, int(width / avg_char_width))
    for line in explicit_lines:
        total_lines += max(1, -(-len(line) // chars_per_line))  # ceil division
    return max(1, total_lines)


def build_design_plan(simple):
    palette = _THEMES.get(simple.theme, _THEMES[_DEFAULT_THEME])
    y = 60
    children = []

    for el in simple.elements:
        if el.type == "heading":
            font_size = 26
            lines = _estimate_wrapped_lines(el.content, _CONTENT_WIDTH, font_size)
            height = lines * 32
            children.append(DesignNode(
                type="text", name="heading", x=_MARGIN, y=y,
                width=_CONTENT_WIDTH, height=height, content=el.content,
                font_size=font_size, color=_rgb(palette.heading),
            ))
            y += height + 24

        elif el.type == "text":
            font_size = 15
            lines = _estimate_wrapped_lines(el.content, _CONTENT_WIDTH, font_size)
            height = lines * 22
            children.append(DesignNode(
                type="text", name="text_line", x=_MARGIN, y=y,
                width=_CONTENT_WIDTH, height=height, content=el.content,
                font_size=font_size, color=_rgb(palette.text),
            ))
            y += height + 16

        elif el.type == "input":
            children.append(DesignNode(
                type="text", name="input_label", x=_MARGIN, y=y,
                width=_CONTENT_WIDTH, height=18, content=el.content,
                font_size=13, color=_rgb(palette.label),
            ))
            y += 18 + 6
            children.append(DesignNode(
                type="rectangle", name="input_field", x=_MARGIN, y=y,
                width=_CONTENT_WIDTH, height=46, content="",
                corner_radius=10, color=_rgb(palette.input_bg),
                effects=[_soft_shadow(palette)],
            ))
            y += 46 + 24

        elif el.type == "button":
            btn_height = 50
            children.append(DesignNode(
                type="rectangle", name="button_bg", x=_MARGIN, y=y,
                width=_CONTENT_WIDTH, height=btn_height, content="",
                corner_radius=25, color=_rgb(palette.button_bg),
                effects=[_strong_shadow(palette)],
            ))
            children.append(DesignNode(
                type="text", name="button_label",
                x=_MARGIN, y=y + (btn_height // 2) - 10,
                width=_CONTENT_WIDTH, height=20, content=el.content,
                font_size=16, color=_rgb(palette.button_text),
            ))
            y += btn_height + 24

    frame_height = max(812, y + 60)
    frame = DesignNode(
        type="frame",
        name=re.sub(r"[^a-zA-Z0-9]+", "_", simple.screen_name.strip().lower()) or "screen",
        x=0, y=0, width=_FRAME_WIDTH, height=frame_height,
        color=_rgb(palette.bg),
        children=children,
    )
    return DesignPlan(screen_name=simple.screen_name, elements=[frame])

# ---------------------------------------------------------------------------
# Planner interface + backends
# ---------------------------------------------------------------------------

class Planner(ABC):
    @abstractmethod
    async def generate_plan(self, prompt):
        ...


class OllamaPlanner(Planner):
    def __init__(self, base_url, model, timeout=45.0):
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout

    async def _call_once(self, prompt, nudge):
        user_content = prompt
        if nudge:
            user_content = f"{prompt}\n\n(Reminder: every content value must be specific real text, never the word 'string', 'text', or 'label', and never empty.)"
        body = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            "format": "json",
            "stream": False,
            "keep_alive": "10m",
            "options": {"temperature": 0.4, "num_predict": 700},
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

    async def generate_plan(self, prompt):
        last_error = None
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                simple = await self._call_once(prompt, nudge=(attempt > 1))
                if attempt > 1:
                    logger.info("OllamaPlanner succeeded on retry attempt %d", attempt)
                return build_design_plan(simple)
            except Exception as exc:
                last_error = exc
                logger.warning("OllamaPlanner attempt %d/%d failed: %s", attempt, _MAX_RETRIES, exc)
        raise last_error

    async def is_available(self):
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.get(f"{self._base_url}/api/tags")
                return resp.status_code == 200
        except (httpx.ConnectError, httpx.TimeoutException):
            return False


class AnthropicPlanner(Planner):
    _API_URL = "https://api.anthropic.com/v1/messages"
    _MODEL = "claude-sonnet-4-5"

    def __init__(self, api_key):
        self._api_key = api_key

    async def generate_plan(self, prompt):
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
    async def generate_plan(self, prompt):
        p = prompt.lower()
        if "login" in p or "sign in" in p or "log in" in p:
            return _login_template()
        if "dashboard" in p:
            return _dashboard_template()
        # Generic fallback: build a minimal placeholder screen instead of
        # failing outright, so the tool always returns SOMETHING usable.
        return _generic_fallback_template(prompt)


def _login_template():
    simple = SimplePlan(screen_name="Login Screen", theme=_DEFAULT_THEME, elements=[
        SimpleElement(type="heading", content="Welcome Back"),
        SimpleElement(type="input", content="Email"),
        SimpleElement(type="input", content="Password"),
        SimpleElement(type="button", content="Log In"),
    ])
    return build_design_plan(simple)


def _dashboard_template():
    simple = SimplePlan(screen_name="Dashboard", theme=_DEFAULT_THEME, elements=[
        SimpleElement(type="heading", content="Dashboard"),
        SimpleElement(type="text", content="Revenue: $12,400"),
        SimpleElement(type="text", content="Active Users: 342"),
        SimpleElement(type="button", content="View Report"),
    ])
    return build_design_plan(simple)


def _generic_fallback_template(prompt):
    title = prompt.strip().split(".")[0][:40] or "New Screen"
    simple = SimplePlan(screen_name=title, theme=_DEFAULT_THEME, elements=[
        SimpleElement(type="heading", content=title),
        SimpleElement(type="text", content="Content could not be auto-generated for this request."),
        SimpleElement(type="button", content="Continue"),
    ])
    return build_design_plan(simple)


class FallbackPlanner(Planner):
    def __init__(self, primary, fallback):
        self._primary = primary
        self._fallback = fallback

    async def generate_plan(self, prompt):
        try:
            return await self._primary.generate_plan(prompt)
        except Exception as exc:
            logger.warning(
                "Primary planner (%s) failed after retries: %s. Falling back to %s.",
                type(self._primary).__name__, exc, type(self._fallback).__name__,
            )
            return await self._fallback.generate_plan(prompt)


def get_planner():
    ollama = OllamaPlanner(
        base_url=config.OLLAMA_BASE_URL,
        model=config.OLLAMA_MODEL,
    )
    return FallbackPlanner(primary=ollama, fallback=TemplatePlanner())