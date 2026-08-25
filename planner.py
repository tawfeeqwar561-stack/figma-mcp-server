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

import components
import config
from design_plan import AutoLayoutConfig, DesignPlan, DesignNode, ColorRGB, EffectConfig
from design_tokens import DEFAULT_STYLE, STYLE_NAMES, get_tokens

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
#
# NOTE ON BACKWARD COMPATIBILITY: this schema is intentionally shared by
# BOTH the legacy flat engine (build_design_plan, still used byte-for-byte
# by TemplatePlanner/_login_template/_dashboard_template -- several tests
# hardcode their exact node counts) and the new semantic engine
# (build_semantic_plan, used by OllamaPlanner/AnthropicPlanner). Every new
# field below has a safe default and every new "type" value is additive,
# so the legacy engine's own construction calls (which only ever use
# "heading"/"text"/"input"/"button" with just `content` set) are completely
# unaffected by this expansion.
# ---------------------------------------------------------------------------

class SimpleElement(BaseModel):
    type: Literal[
        "heading", "text", "paragraph", "label", "button", "input", "form",
        "card", "stat_card", "header", "nav", "sidebar", "list", "table",
        "badge", "avatar", "tabs", "divider", "image", "icon", "section",
    ]
    # Relaxed from a bare required field to default="" so semantic types
    # that carry their meaning in `items`/`rows` instead (table, divider,
    # nav, sidebar, tabs, badge, list) don't need a placeholder value here.
    # Tighter than DesignNode.content since this is pre-layout-expansion
    # raw model output -- see H-6 in bridge-security-hardening.
    content: str = Field(default="", max_length=500)
    # Additive: repeated labels -- nav/sidebar links, tabs, badges, list
    # item titles, button-group labels, or a table's header row. Capped to
    # keep prompt-level intent complexity (not final node count, which is
    # still bounded by DesignNode/DesignPlan caps) bounded per H-6.
    items: list[str] = Field(default_factory=list, max_length=12)
    # Additive: table body rows only (each inner list is one row's cells).
    rows: list[list[str]] = Field(default_factory=list, max_length=8)
    # Additive: secondary text -- a stat_card's trend line, an input's
    # placeholder, a card's body copy, a form's submit button label.
    subtitle: str = Field(default="", max_length=500)
    # Additive: heading size. Ignored by every other type.
    level: Literal["display", "h1", "h2", "h3"] = "h1"
    # Additive: free-text hint (button variant / badge tone). Unknown
    # values fall back safely to a sensible default in components.py.
    variant: str = ""


class SimplePlan(BaseModel):
    screen_name: str
    theme: str = _DEFAULT_THEME
    # Turns the "cap at 10 elements" prompt instruction into a real,
    # enforced ceiling -- see H-6 in bridge-security-hardening.
    elements: list[SimpleElement] = Field(min_length=1, max_length=10)


_THEME_NAMES = ", ".join(f'"{k}"' for k in STYLE_NAMES)

_SYSTEM_PROMPT = f"""You are a senior UI/UX designer. Read the user's screen request and output a JSON object listing the screen name, a visual style, and the elements it needs, in top-to-bottom order.

Output EXACTLY this shape, nothing else:
{{
  "screen_name": "<short title for this screen, specific to the request>",
  "theme": "<one of: {_THEME_NAMES}>",
  "elements": [
    {{"type": "<see element types below>", "content": "<primary text, if this type needs it>", "items": ["<repeated label>", "..."], "rows": [["<cell>", "..."]], "subtitle": "<secondary text, if needed>", "level": "h1", "variant": ""}}
  ]
}}
Omit any of content/items/rows/subtitle/level/variant an element does not need -- they all default to empty.

Style selection guide:
- "professional_blue": default, business/finance/productivity/general apps.
- "midnight_premium": premium, luxury, dark-mode, "sleek/modern" requests.
- "calm_wellness": health, wellness, meditation, fitness, therapy.
- "warm_friendly": social, community, food, casual, friendly-toned.
- "minimal_saas": "minimal SaaS dashboard" or clean/minimal B2B software requests.
- "dark_fintech": "dark fintech dashboard", trading, crypto, banking-at-night requests.
- "modern_ecommerce": "modern ecommerce", shopping, retail, marketplace requests.
- "healthcare_mobile": "healthcare mobile app", medical, clinical, patient-facing requests.

Element types:
- "heading": a large title (set "level" to "display"/"h1"/"h2"/"h3"). Page titles, welcome messages, section titles.
- "text" or "paragraph": a line or short block of body copy/description.
- "label": a small standalone caption-style line.
- "button": one tappable action in "content" (e.g. "Log In"). For several buttons in a row (e.g. "quick actions", "Send"/"Request"), put ALL labels in "items" instead and leave "content" empty.
- "input": one labeled field the user types into. "content" = the label (e.g. "Email"), "subtitle" = placeholder text if any.
- "form": several input fields plus a submit button. "items" = one label per field (e.g. ["Email","Password"]), "subtitle" = the submit button's label (e.g. "Log In").
- "card": a bordered content block. "content" = its title, "subtitle" = its body copy.
- "stat_card": one KPI number. "content" = the metric label (e.g. "Revenue"), "subtitle" = the value (e.g. "$12,400"), "items" = [a one-item trend like "+4.2%"] if relevant.
- "header": the page's top bar. "content" = the app/page name, "items" = nav link labels (e.g. ["Dashboard","Reports","Settings"]).
- "nav": same as "header" -- use whichever reads more naturally.
- "sidebar": a vertical nav menu. "items" = one label per menu entry.
- "list": a vertical list of rows. "items" = one line of text per row (e.g. recent transactions, notifications).
- "table": tabular data. "items" = column headers, "rows" = each data row as an array of cell strings matching the header count. Keep it small (3-6 rows) unless the request implies more.
- "badge": one or more small status/tag pills. "content" = a single badge's text, OR "items" = several badge texts. "variant" = "success"|"warning"|"error"|"primary"|"neutral".
- "avatar": a user avatar. "content" = initials (1-2 letters).
- "tabs": a tab strip. "items" = one label per tab.
- "divider": a plain horizontal rule. No content needed.
- "image": a placeholder image block. "content" = its caption (e.g. "Product photo").
- "icon": a small icon glyph. "content" = 1-2 characters representing it.
- "section": a titled group wrapping a short description. "content" = section title, "subtitle" = a one-line description.

RULES:
- Every text value you DO provide (content/items/subtitle) must be specific and realistic (invent plausible real values like amounts, names, dates where the request implies data).
- Never write the literal words "string", "text", or "label" as a value.
- Cap the total number of elements at 10, even if the request could imply more -- prefer one "table"/"list"/"nav" element with several "items"/"rows" over many separate elements.
- Ignore purely stylistic/tone instructions beyond picking the style (e.g. "clean typography") — they do not map to elements.
- Do not include x, y, width, height, or color — only the fields shown above.
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
# Types allowed to have an empty `content` because their real meaning
# lives in `items`/`rows` instead (see SimpleElement docstring above).
_CONTENT_OPTIONAL_TYPES = {"divider", "table", "sidebar", "tabs", "list", "nav", "header", "form", "badge"}


def _check_semantic_quality(plan):
    if plan.screen_name.strip().lower() in _PLACEHOLDER_MARKERS:
        raise ValueError(f"Placeholder leakage in screen_name: {plan.screen_name!r}")
    for el in plan.elements:
        content_marker = el.content.strip().lower()
        if content_marker in _PLACEHOLDER_MARKERS and el.type not in _CONTENT_OPTIONAL_TYPES:
            raise ValueError(f"Placeholder leakage in element content: {el.content!r}")
        for item in el.items:
            if item.strip().lower() in _PLACEHOLDER_MARKERS:
                raise ValueError(f"Placeholder leakage in element items: {item!r}")
        for row in el.rows:
            for cell in row:
                if cell.strip().lower() in _PLACEHOLDER_MARKERS:
                    raise ValueError(f"Placeholder leakage in table row cell: {cell!r}")


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
# Semantic layout engine v2 -- understands UI intent (cards, nav, forms,
# lists, tables, headers, sidebars, etc), builds through Auto Layout via
# components.py, and draws every color/size/weight from one DesignTokens
# instance per screen (design_tokens.py) instead of ad hoc per-node values.
#
# Additive: build_design_plan (above) is completely untouched and still
# used byte-for-byte by TemplatePlanner/_login_template/_dashboard_template/
# _generic_fallback_template -- several existing tests hardcode their exact
# node counts (8 for login, 6 for dashboard), so that path must never
# change. Only OllamaPlanner/AnthropicPlanner call this new engine.
# ---------------------------------------------------------------------------

_MOBILE_KEYWORDS = ("mobile", "iphone", "ios", "android", "smartphone")
_DESKTOP_KEYWORDS = ("desktop", "web app", "webapp", "browser", "web application")
_DESKTOP_HINT_KEYWORDS = ("dashboard", "admin panel", "admin", "analytics", "back office", "backoffice")
_CONTINUITY_KEYWORDS = (
    "same design", "same style", "same system", "same theme", "matching design",
    "matching style", "matching system", "consistent design", "consistent style",
    "consistent with", "same design system", "like the previous", "match the previous",
    "matching screen", "same look", "same visual", "same visual system",
    # Broader, single-word signal covering natural phrasing like "a
    # matching login screen" or "matching profile page" that the
    # multi-word phrases above don't literally contain.
    "matching",
)


def detect_platform(prompt: str) -> str:
    """
    Deterministic keyword classifier for screen size -- NOT decided by the
    LLM, so it can never hallucinate an inconsistent size, and screens are
    no longer hardcoded to one mobile width (see components.PLATFORM_SIZES).
    Falls back to "desktop" for dashboard/admin-flavored requests and
    "mobile" otherwise (matching the single-purpose auth/profile/settings
    screens this project has historically generated at 375x812).
    """
    p = prompt.lower()
    if any(re.search(rf"\b{re.escape(kw)}\b", p) for kw in _MOBILE_KEYWORDS):
        return "mobile"
    if any(re.search(rf"\b{re.escape(kw)}\b", p) for kw in _DESKTOP_KEYWORDS):
        return "desktop"
    if any(kw in p for kw in _DESKTOP_HINT_KEYWORDS):
        return "desktop"
    return "mobile"


def wants_matching_style(prompt: str) -> bool:
    """
    Deterministic keyword detector for Phase 10 (multi-prompt agent
    behavior): "now create a matching login screen", "using the same
    design system", etc. Deterministic rather than LLM-decided so
    continuity can never silently fail to apply.
    """
    p = prompt.lower()
    return any(kw in p for kw in _CONTINUITY_KEYWORDS)


# Process-wide memory of the last DesignTokens actually used, so a follow-up
# prompt requesting "the same design system" reuses it exactly. As simple
# as the rest of this project's existing single-shared-state model (one
# shared bridge token, one shared Figma file, documented in README's "Known
# limitations") -- a real multi-user/multi-conversation deployment would
# key this by session/user instead of a bare module global.
_last_design_tokens = None


def _remember_tokens(tokens) -> None:
    global _last_design_tokens
    _last_design_tokens = tokens


def _resolve_tokens(style_name: str, prompt: str):
    if wants_matching_style(prompt) and _last_design_tokens is not None:
        logger.info("Reusing previous design system (%s) for style continuity.", _last_design_tokens.name)
        return _last_design_tokens
    return get_tokens(style_name)


def build_semantic_plan(simple: SimplePlan, prompt: str = "") -> DesignPlan:
    """
    Turn a SimplePlan (LLM content + intent) into a fully-composed,
    Auto-Layout DesignPlan tree using components.py, with one coherent
    DesignTokens instance driving every color/size/weight on the screen.
    """
    tokens = _resolve_tokens(simple.theme, prompt)
    platform = detect_platform(prompt)
    width, _height = components.PLATFORM_SIZES.get(platform, components.PLATFORM_SIZES["desktop"])

    has_sidebar = platform == "desktop" and any(el.type == "sidebar" for el in simple.elements)
    inner_width = max(240, width - (2 * tokens.spacing.xl) - (tokens.component_sizes.sidebar_width if has_sidebar else 0))

    header_node: DesignNode | None = None
    sidebar_node: DesignNode | None = None
    content: list[DesignNode] = []
    stat_buffer: list[DesignNode] = []

    def _flush_stat_buffer() -> None:
        if not stat_buffer:
            return
        if len(stat_buffer) == 1:
            content.append(stat_buffer[0])
        else:
            content.append(DesignNode(
                type="frame", name="stat_row", width=inner_width, height=120,
                auto_layout=AutoLayoutConfig(
                    direction="HORIZONTAL", spacing=tokens.spacing.md, padding=0,
                    primary_axis_sizing="FIXED", counter_axis_sizing="AUTO",
                ),
                children=list(stat_buffer), semantic="stat_row",
            ))
        stat_buffer.clear()

    for el in simple.elements:
        if el.type in ("header", "nav"):
            if header_node is None:
                nav_items = (el.items or None) if platform == "desktop" else None
                header_node = components.top_nav(tokens, el.content or simple.screen_name, nav_items=nav_items, width=width)
            continue

        if el.type == "sidebar":
            if platform == "desktop":
                if sidebar_node is None:
                    sidebar_node = components.sidebar(tokens, el.items or ["Home"])
            else:
                # No persistent-sidebar pattern on mobile -- render as a
                # plain nav list in the content column instead of
                # silently dropping the user's intent.
                _flush_stat_buffer()
                content.append(components.list_block(
                    tokens, [{"title": t} for t in (el.items or ["Home"])],
                    width=inner_width, name="mobile_nav_list",
                ))
            continue

        if el.type != "stat_card":
            _flush_stat_buffer()

        node: DesignNode | None = None
        if el.type == "heading":
            node = components.heading(tokens, el.content or simple.screen_name, level=el.level, width=inner_width)
        elif el.type in ("text", "paragraph"):
            node = components.paragraph(tokens, el.content or simple.screen_name, width=inner_width)
        elif el.type == "label":
            node = components.label_text(tokens, el.content or "Label")
        elif el.type == "button":
            if el.items:
                node = components.button_group(tokens, el.items, variant=(el.variant or "primary"))
            else:
                node = components.button(tokens, el.content or "Continue", variant=(el.variant or "primary"))
        elif el.type == "input":
            node = components.input_field(tokens, label=el.content or None, placeholder=el.subtitle, width=inner_width)
        elif el.type == "form":
            fields = [(field_label, "") for field_label in (el.items or [el.content or "Field"])]
            node = components.form(tokens, fields, submit_label=el.subtitle or "Submit", width=inner_width)
        elif el.type == "card":
            card_children = []
            if el.content:
                card_children.append(components.heading(tokens, el.content, level="h3"))
            if el.subtitle:
                card_children.append(components.paragraph(tokens, el.subtitle, width=inner_width - (2 * tokens.component_sizes.card_padding)))
            node = components.card(tokens, card_children or [components.paragraph(tokens, "", width=inner_width)], width=inner_width)
        elif el.type == "stat_card":
            stat_node = components.stat_card(tokens, el.content or "Metric", el.subtitle or "--", delta=(el.items[0] if el.items else None))
            stat_buffer.append(stat_node)
        elif el.type == "list":
            items = [{"title": t} for t in (el.items or [el.content or "Item"])]
            node = components.list_block(tokens, items, width=inner_width)
        elif el.type == "table":
            node = components.table(tokens, el.items or ["Column 1"], el.rows, width=inner_width)
        elif el.type == "badge":
            tone = el.variant if el.variant in ("success", "warning", "error", "primary", "neutral") else "neutral"
            if el.items:
                node = components.badge_row(tokens, el.items, tone=tone)
            else:
                node = components.badge(tokens, el.content or "Status", tone=tone)
        elif el.type == "avatar":
            node = components.avatar(tokens, initials=el.content or "?")
        elif el.type == "tabs":
            node = components.tabs(tokens, el.items or [el.content or "Tab"])
        elif el.type == "divider":
            node = components.divider(tokens)
        elif el.type == "image":
            node = components.image_block(tokens, caption=el.content or "Image", width=inner_width)
        elif el.type == "icon":
            node = components.icon_glyph(tokens, glyph=el.content)
        elif el.type == "section":
            sec_children = [components.paragraph(tokens, el.subtitle, width=inner_width)] if el.subtitle else []
            node = components.section(tokens, el.content or None, sec_children, width=inner_width)

        if node is not None:
            content.append(node)

    _flush_stat_buffer()

    root = components.page(
        tokens, simple.screen_name, platform=platform,
        header=header_node, sidebar_node=sidebar_node, content=content,
    )
    _remember_tokens(tokens)
    return DesignPlan(screen_name=simple.screen_name, elements=[root], design_system=tokens.name)


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
                return build_semantic_plan(simple, prompt=prompt)
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
        return build_semantic_plan(simple, prompt=prompt)


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