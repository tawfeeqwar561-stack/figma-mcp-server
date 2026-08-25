"""
Semantic component composers.

Each function here builds a DesignNode (or small tree of DesignNodes) for
a recognizable UI concept -- heading, button, card, sidebar, table, etc --
using ONLY the existing primitive NodeTypes (frame/text/rectangle/ellipse/
component/instance) plus AutoLayoutConfig, drawing every color/size/weight
from a single DesignTokens instance (design_tokens.py).

This is deliberately a pure, plugin-agnostic layer: nothing here knows
about bridge_client or the WebSocket protocol. plan_executor.py already
knows how to walk whatever DesignNode tree comes out of here.

Design principles used throughout:
  - Auto Layout by default. Every container composer sets `auto_layout`;
    manual x/y is only ever used for the couple of places Figma requires a
    concrete starting size (handled internally, invisible to callers).
  - One shared DesignTokens instance per screen. No composer ever invents
    its own color or font size -- everything is read from `tokens`.
  - Real component/instance reuse for the safe, unambiguous case: a
    repeated atom whose only difference between repeats is a single text
    label (buttons in a button group, badges in a row, title-only list
    rows). See `_component_or_instance`. Anything with more than one
    varying part (icon + label, avatar + title + subtitle, active/inactive
    state) is composed as plain frames instead -- still fully valid,
    hierarchical, auto-layout output, just not instance-deduplicated.
"""

from __future__ import annotations

import re
from typing import Literal

from design_plan import AutoLayoutConfig, DesignNode
from design_tokens import DesignTokens

PLATFORM_SIZES: dict[str, tuple[int, int]] = {
    "mobile": (375, 812),
    "desktop": (1440, 1024),
}


def slugify(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "_", text.strip().lower()).strip("_") or "node"


# ---------------------------------------------------------------------------
# Typography
# ---------------------------------------------------------------------------

_HEADING_LEVELS = {"display", "h1", "h2", "h3"}


def heading(tokens: DesignTokens, text: str, level: str = "h1", width: int | None = None, name: str | None = None) -> DesignNode:
    style = getattr(tokens.typography, level if level in _HEADING_LEVELS else "h1")
    return DesignNode(
        type="text", name=name or f"heading_{level}", content=text,
        font_size=style.size, font_weight=style.weight,
        text_color=tokens.colors.text_primary,
        width=width or 320, height=int(style.size * style.line_height_multiplier),
        text_auto_resize="WIDTH_AND_HEIGHT" if width is None else "HEIGHT",
        semantic="heading",
    )


def paragraph(tokens: DesignTokens, text: str, width: int = 320, name: str = "paragraph") -> DesignNode:
    style = tokens.typography.body
    lines = _estimate_wrapped_lines(text, width, style.size)
    return DesignNode(
        type="text", name=name, content=text,
        font_size=style.size, font_weight=style.weight,
        text_color=tokens.colors.text_secondary,
        width=width, height=int(lines * style.size * style.line_height_multiplier),
        text_auto_resize="HEIGHT", semantic="paragraph",
    )


def label_text(tokens: DesignTokens, text: str, name: str = "label") -> DesignNode:
    style = tokens.typography.label
    return DesignNode(
        type="text", name=name, content=text, font_size=style.size, font_weight=style.weight,
        text_color=tokens.colors.text_secondary, width=200, height=int(style.size * 1.3),
        text_auto_resize="WIDTH_AND_HEIGHT", semantic="label",
    )


def caption_text(tokens: DesignTokens, text: str, name: str = "caption", color=None) -> DesignNode:
    style = tokens.typography.caption
    return DesignNode(
        type="text", name=name, content=text, font_size=style.size, font_weight=style.weight,
        text_color=color or tokens.colors.text_secondary, width=200, height=int(style.size * 1.3),
        text_auto_resize="WIDTH_AND_HEIGHT", semantic="caption",
    )


def _estimate_wrapped_lines(content: str, width: int, font_size: int) -> int:
    """Same heuristic the original planner.py used: ~0.55x font_size average
    character width. Approximate, not pixel-perfect, but bounds text-block
    height sanely without measuring real font metrics (unavailable here)."""
    explicit_lines = content.split("\n")
    avg_char_width = font_size * 0.55
    chars_per_line = max(1, int(width / avg_char_width))
    total = 0
    for line in explicit_lines:
        total += max(1, -(-len(line) // chars_per_line))
    return max(1, total)


# ---------------------------------------------------------------------------
# Component/instance reuse helper
# ---------------------------------------------------------------------------

def _component_or_instance(*, is_first: bool, register_as: str, full_node: DesignNode, override_text: str | None) -> DesignNode:
    """
    First repeat of an atom becomes a real `component` (registered under
    `register_as`); every subsequent repeat becomes a lightweight
    `instance` pointing back at it, with only its text content overridden.
    Only safe/used for atoms whose repeats vary by nothing but one text
    label -- see module docstring.
    """
    if is_first:
        full_node.type = "component"
        full_node.register_as = register_as
        return full_node
    return DesignNode(
        type="instance", name=full_node.name, x=full_node.x, y=full_node.y,
        width=full_node.width, height=full_node.height,
        component_ref=register_as, content=override_text or "",
        layout_align=full_node.layout_align, layout_grow=full_node.layout_grow,
        semantic=full_node.semantic,
    )


# ---------------------------------------------------------------------------
# Buttons
# ---------------------------------------------------------------------------

ButtonVariant = Literal["primary", "secondary", "outline", "ghost"]


def _button_variant_colors(tokens: DesignTokens, variant: ButtonVariant):
    c = tokens.colors
    if variant == "primary":
        return c.primary, c.on_primary, None
    if variant == "secondary":
        return c.secondary, c.on_secondary, None
    if variant == "outline":
        return c.surface, c.text_primary, c.border
    return None, c.primary, None  # ghost: no fill, primary-colored label


def button(tokens: DesignTokens, text: str, variant: ButtonVariant = "primary", stretch: bool = False, name: str = "button") -> DesignNode:
    fill, text_color, stroke = _button_variant_colors(tokens, variant)
    h = tokens.component_sizes.button_height
    label = DesignNode(
        type="text", name="button_label", content=text,
        font_size=tokens.typography.button.size, font_weight=tokens.typography.button.weight,
        text_color=text_color, width=120, height=int(tokens.typography.button.size * 1.2),
    )
    return DesignNode(
        type="frame", name=name, width=120, height=h, color=fill,
        stroke_color=stroke, stroke_weight=1.0 if stroke else 1.0,
        corner_radius=tokens.radius.md,
        auto_layout=AutoLayoutConfig(
            direction="HORIZONTAL", spacing=8,
            padding_top=0, padding_bottom=0, padding_left=20, padding_right=20,
            align_items="CENTER", counter_axis_align="CENTER",
            primary_axis_sizing="AUTO", counter_axis_sizing="AUTO",
        ),
        layout_align="STRETCH" if stretch else None,
        effects=[tokens.shadow_soft] if variant == "primary" else [],
        children=[label], semantic="button",
    )


def button_group(tokens: DesignTokens, labels: list[str], variant: ButtonVariant = "primary", name: str = "button_group") -> DesignNode:
    register_as = f"{name}_{slugify(labels[0]) if labels else 'btn'}_component"
    children = []
    for i, text in enumerate(labels):
        full = button(tokens, text, variant=variant, name=f"button_{i}")
        children.append(_component_or_instance(is_first=(i == 0), register_as=register_as, full_node=full, override_text=text))
    return DesignNode(
        type="frame", name=name, width=200, height=tokens.component_sizes.button_height,
        auto_layout=AutoLayoutConfig(direction="HORIZONTAL", spacing=tokens.spacing.sm, padding=0, primary_axis_sizing="AUTO", counter_axis_sizing="AUTO"),
        children=children, semantic="button_group",
    )


# ---------------------------------------------------------------------------
# Inputs / forms
# ---------------------------------------------------------------------------

def input_field(tokens: DesignTokens, label: str | None = None, placeholder: str = "", width: int = 320, name: str = "input") -> DesignNode:
    h = tokens.component_sizes.input_height
    box_children = [DesignNode(
        type="text", name="placeholder", content=placeholder or "",
        font_size=tokens.typography.body.size, font_weight="Regular",
        text_color=tokens.colors.text_secondary if placeholder else tokens.colors.text_disabled,
        width=width - 32, height=int(tokens.typography.body.size * 1.3),
        text_auto_resize="HEIGHT",
    )]
    box = DesignNode(
        type="frame", name="input_box", width=width, height=h,
        color=tokens.colors.surface_alt, stroke_color=tokens.colors.border, stroke_weight=1.0,
        corner_radius=tokens.radius.sm,
        auto_layout=AutoLayoutConfig(
            direction="HORIZONTAL", spacing=8, padding=16,
            align_items="CENTER", counter_axis_align="CENTER",
            primary_axis_sizing="FIXED", counter_axis_sizing="FIXED",
        ),
        children=box_children, semantic="input_box",
    )
    stack_children = []
    if label:
        stack_children.append(label_text(tokens, label, name="input_label"))
    stack_children.append(box)
    return DesignNode(
        type="frame", name=name, width=width, height=(h + 24 if label else h),
        auto_layout=AutoLayoutConfig(direction="VERTICAL", spacing=6, padding=0, primary_axis_sizing="AUTO", counter_axis_sizing="FIXED"),
        children=stack_children, semantic="input",
    )


def form(tokens: DesignTokens, fields: list[tuple[str, str]], submit_label: str = "Submit", width: int = 320, name: str = "form") -> DesignNode:
    children = [input_field(tokens, label=lbl, placeholder=ph, width=width, name=f"field_{i}") for i, (lbl, ph) in enumerate(fields)]
    children.append(button(tokens, submit_label, variant="primary", stretch=True, name="submit_button"))
    return DesignNode(
        type="frame", name=name, width=width, height=100,
        auto_layout=AutoLayoutConfig(direction="VERTICAL", spacing=tokens.spacing.md, padding=0, primary_axis_sizing="AUTO", counter_axis_sizing="FIXED"),
        children=children, semantic="form",
    )


# ---------------------------------------------------------------------------
# Badges / avatars / dividers / media
# ---------------------------------------------------------------------------

BadgeTone = Literal["neutral", "success", "warning", "error", "primary"]


def _badge_tone_colors(tokens: DesignTokens, tone: BadgeTone):
    c = tokens.colors
    return {
        "neutral": (c.surface_alt, c.text_secondary),
        "success": (c.surface_alt, c.success),
        "warning": (c.surface_alt, c.warning),
        "error": (c.surface_alt, c.error),
        "primary": (c.secondary, c.on_secondary),
    }.get(tone, (c.surface_alt, c.text_secondary))


def badge(tokens: DesignTokens, text: str, tone: BadgeTone = "neutral", name: str = "badge") -> DesignNode:
    fill, text_color = _badge_tone_colors(tokens, tone)
    label = DesignNode(
        type="text", name="badge_label", content=text,
        font_size=tokens.typography.caption.size, font_weight="Medium",
        text_color=text_color, width=80, height=int(tokens.typography.caption.size * 1.2),
    )
    return DesignNode(
        type="frame", name=name, width=80, height=24, color=fill, corner_radius=tokens.radius.full,
        auto_layout=AutoLayoutConfig(
            direction="HORIZONTAL", spacing=4, padding_top=4, padding_bottom=4, padding_left=10, padding_right=10,
            align_items="CENTER", counter_axis_align="CENTER", primary_axis_sizing="AUTO", counter_axis_sizing="AUTO",
        ),
        children=[label], semantic="badge",
    )


def badge_row(tokens: DesignTokens, labels: list[str], tone: BadgeTone = "neutral", name: str = "badge_row") -> DesignNode:
    register_as = f"{name}_component"
    children = []
    for i, text in enumerate(labels):
        full = badge(tokens, text, tone=tone, name=f"badge_{i}")
        children.append(_component_or_instance(is_first=(i == 0), register_as=register_as, full_node=full, override_text=text))
    return DesignNode(
        type="frame", name=name, width=200, height=24,
        auto_layout=AutoLayoutConfig(direction="HORIZONTAL", spacing=tokens.spacing.xs, padding=0, primary_axis_sizing="AUTO", counter_axis_sizing="AUTO"),
        children=children, semantic="badge_row",
    )


def avatar(tokens: DesignTokens, initials: str = "", name: str = "avatar") -> DesignNode:
    size = tokens.component_sizes.avatar_size
    children = []
    if initials:
        children.append(DesignNode(
            type="text", name="avatar_initials", content=initials[:2].upper(),
            font_size=max(10, int(size * 0.38)), font_weight="Bold",
            text_color=tokens.colors.on_secondary, width=size, height=size,
        ))
    return DesignNode(
        type="frame", name=name, width=size, height=size, color=tokens.colors.secondary,
        corner_radius=tokens.radius.full,
        auto_layout=AutoLayoutConfig(direction="HORIZONTAL", spacing=0, padding=0, align_items="CENTER", counter_axis_align="CENTER", primary_axis_sizing="FIXED", counter_axis_sizing="FIXED"),
        children=children, semantic="avatar",
    )


def divider(tokens: DesignTokens, name: str = "divider") -> DesignNode:
    return DesignNode(
        type="rectangle", name=name, width=100, height=1, color=tokens.colors.border,
        layout_align="STRETCH", semantic="divider",
    )


def image_block(tokens: DesignTokens, caption: str = "Image", width: int = 343, height: int = 160, name: str = "image") -> DesignNode:
    return DesignNode(
        type="image_placeholder", name=name, width=width, height=height, content=caption,
        corner_radius=tokens.radius.md, semantic="image",
    )


def icon_glyph(tokens: DesignTokens, glyph: str = "", size: int | None = None, color=None, name: str = "icon") -> DesignNode:
    s = size or tokens.component_sizes.icon_size
    return DesignNode(type="icon", name=name, width=s, height=s, content=glyph, color=color or tokens.colors.secondary, semantic="icon")


# ---------------------------------------------------------------------------
# Cards
# ---------------------------------------------------------------------------

def card(tokens: DesignTokens, children: list[DesignNode], width: int | None = None, name: str = "card", padding: int | None = None) -> DesignNode:
    return DesignNode(
        type="frame", name=name, width=width or 320, height=100,
        color=tokens.colors.surface, corner_radius=tokens.radius.lg,
        stroke_color=tokens.colors.border, stroke_weight=1.0,
        effects=[tokens.shadow_soft],
        auto_layout=AutoLayoutConfig(
            direction="VERTICAL", spacing=tokens.spacing.sm,
            padding=padding if padding is not None else tokens.component_sizes.card_padding,
            primary_axis_sizing="AUTO", counter_axis_sizing="FIXED" if width else "AUTO",
        ),
        children=children, semantic="card",
    )


def stat_card(tokens: DesignTokens, label: str, value: str, delta: str | None = None, tone: BadgeTone = "success", width: int = 220, name: str = "stat_card") -> DesignNode:
    children = [caption_text(tokens, label, name="stat_label"), heading(tokens, value, level="h2", name="stat_value")]
    if delta:
        children.append(badge(tokens, delta, tone=tone, name="stat_delta"))
    return card(tokens, children, width=width, name=name)


# ---------------------------------------------------------------------------
# Lists / tables
# ---------------------------------------------------------------------------

def list_item(tokens: DesignTokens, title: str, subtitle: str | None = None, trailing: str | None = None, show_avatar: bool = False, initials: str = "", name: str = "list_item") -> DesignNode:
    row_children = []
    if show_avatar:
        row_children.append(avatar(tokens, initials=initials or title[:1]))
    text_children = [DesignNode(
        type="text", name="item_title", content=title, font_size=tokens.typography.body.size, font_weight="Medium",
        text_color=tokens.colors.text_primary, width=200, height=int(tokens.typography.body.size * 1.3),
        text_auto_resize="HEIGHT",
    )]
    if subtitle:
        text_children.append(DesignNode(
            type="text", name="item_subtitle", content=subtitle, font_size=tokens.typography.small.size, font_weight="Regular",
            text_color=tokens.colors.text_secondary, width=200, height=int(tokens.typography.small.size * 1.3),
            text_auto_resize="HEIGHT",
        ))
    text_stack = DesignNode(
        type="frame", name="item_text", width=200, height=40,
        auto_layout=AutoLayoutConfig(direction="VERTICAL", spacing=2, padding=0, primary_axis_sizing="AUTO", counter_axis_sizing="FIXED"),
        layout_grow=1, layout_align="STRETCH",
        children=text_children,
    )
    row_children.append(text_stack)
    if trailing:
        row_children.append(caption_text(tokens, trailing, name="item_trailing"))
    return DesignNode(
        type="frame", name=name, width=360, height=56,
        auto_layout=AutoLayoutConfig(
            direction="HORIZONTAL", spacing=tokens.spacing.sm, padding=tokens.spacing.sm,
            align_items="CENTER", counter_axis_align="CENTER", primary_axis_sizing="AUTO", counter_axis_sizing="FIXED",
        ),
        children=row_children, semantic="list_item",
    )


def list_block(tokens: DesignTokens, items: list[dict], show_avatar: bool = False, width: int = 360, name: str = "list") -> DesignNode:
    """
    `items` is a list of dicts with keys title (required), subtitle,
    trailing (optional). Title-only rows (no subtitle/trailing/avatar) are
    deduplicated as component+instance repeats; anything richer is composed
    as plain frames (see module docstring for why).
    """
    simple_repeat = not show_avatar and all(not it.get("subtitle") and not it.get("trailing") for it in items)
    register_as = f"{name}_item_component"
    children = []
    for i, item in enumerate(items):
        if simple_repeat:
            full = list_item(tokens, item["title"], name=f"item_{i}")
            children.append(_component_or_instance(is_first=(i == 0), register_as=register_as, full_node=full, override_text=item["title"]))
        else:
            children.append(list_item(
                tokens, item["title"], subtitle=item.get("subtitle"), trailing=item.get("trailing"),
                show_avatar=show_avatar, initials=item.get("initials", ""), name=f"item_{i}",
            ))
        if i < len(items) - 1:
            children.append(divider(tokens, name=f"divider_{i}"))
    return DesignNode(
        type="frame", name=name, width=width, height=200,
        color=tokens.colors.surface, corner_radius=tokens.radius.lg,
        stroke_color=tokens.colors.border, stroke_weight=1.0,
        auto_layout=AutoLayoutConfig(direction="VERTICAL", spacing=0, padding=0, primary_axis_sizing="AUTO", counter_axis_sizing="FIXED"),
        children=children, semantic="list",
    )


def table(tokens: DesignTokens, headers: list[str], rows: list[list[str]], width: int = 600, name: str = "table") -> DesignNode:
    col_width = max(80, width // max(1, len(headers)))

    def _row(cells: list[str], is_header: bool, zebra: bool) -> DesignNode:
        cell_nodes = []
        for cell in cells:
            style = tokens.typography.label if is_header else tokens.typography.body
            cell_nodes.append(DesignNode(
                type="text", name="cell", content=cell, font_size=style.size,
                font_weight="Bold" if is_header else "Regular",
                text_color=tokens.colors.text_primary if is_header else tokens.colors.text_secondary,
                width=col_width - 16, height=int(style.size * 1.3), text_auto_resize="HEIGHT",
            ))
        return DesignNode(
            type="frame", name="header_row" if is_header else "row", width=width, height=44,
            color=tokens.colors.surface_alt if (is_header or zebra) else tokens.colors.surface,
            auto_layout=AutoLayoutConfig(
                direction="HORIZONTAL", spacing=0, padding_top=10, padding_bottom=10, padding_left=8, padding_right=8,
                align_items="CENTER", counter_axis_align="CENTER", primary_axis_sizing="FIXED", counter_axis_sizing="FIXED",
            ),
            children=cell_nodes, semantic="table_header_row" if is_header else "table_row",
        )

    children = [_row(headers, is_header=True, zebra=False)]
    for i, row in enumerate(rows):
        children.append(_row(row, is_header=False, zebra=(i % 2 == 1)))
    return DesignNode(
        type="frame", name=name, width=width, height=200,
        color=tokens.colors.surface, corner_radius=tokens.radius.lg,
        stroke_color=tokens.colors.border, stroke_weight=1.0,
        auto_layout=AutoLayoutConfig(direction="VERTICAL", spacing=0, padding=0, primary_axis_sizing="AUTO", counter_axis_sizing="FIXED"),
        children=children, semantic="table",
    )


# ---------------------------------------------------------------------------
# Navigation / structure
# ---------------------------------------------------------------------------

def tabs(tokens: DesignTokens, labels: list[str], active_index: int = 0, name: str = "tabs") -> DesignNode:
    children = []
    for i, text in enumerate(labels):
        is_active = i == active_index
        label = DesignNode(
            type="text", name="tab_label", content=text,
            font_size=tokens.typography.label.size, font_weight="Bold" if is_active else "Regular",
            text_color=tokens.colors.primary if is_active else tokens.colors.text_secondary,
            width=100, height=int(tokens.typography.label.size * 1.3), text_auto_resize="WIDTH_AND_HEIGHT",
        )
        indicator = DesignNode(
            type="rectangle", name="tab_indicator", width=100, height=2,
            color=tokens.colors.primary if is_active else tokens.colors.background,
            layout_align="STRETCH",
        )
        children.append(DesignNode(
            type="frame", name=f"tab_{i}", width=100, height=40,
            auto_layout=AutoLayoutConfig(direction="VERTICAL", spacing=8, padding=0, align_items="CENTER", counter_axis_align="CENTER", primary_axis_sizing="AUTO", counter_axis_sizing="AUTO"),
            children=[label, indicator], semantic="tab",
        ))
    return DesignNode(
        type="frame", name=name, width=300, height=40,
        auto_layout=AutoLayoutConfig(direction="HORIZONTAL", spacing=tokens.spacing.lg, padding=0, primary_axis_sizing="AUTO", counter_axis_sizing="AUTO"),
        children=children, semantic="tabs",
    )


def top_nav(tokens: DesignTokens, title: str, nav_items: list[str] | None = None, width: int = 1440, active_index: int = 0, name: str = "header") -> DesignNode:
    nav_children = []
    for i, item in enumerate(nav_items or []):
        nav_children.append(DesignNode(
            type="text", name="nav_link", content=item,
            font_size=tokens.typography.body.size, font_weight="Medium" if i == active_index else "Regular",
            text_color=tokens.colors.text_primary if i == active_index else tokens.colors.text_secondary,
            width=80, height=int(tokens.typography.body.size * 1.3), text_auto_resize="WIDTH_AND_HEIGHT",
        ))
    nav_row = DesignNode(
        type="frame", name="nav_links", width=300, height=24,
        auto_layout=AutoLayoutConfig(direction="HORIZONTAL", spacing=tokens.spacing.lg, padding=0, align_items="CENTER", primary_axis_sizing="AUTO", counter_axis_sizing="AUTO"),
        children=nav_children,
    ) if nav_items else None

    row_children = [heading(tokens, title, level="h3", name="header_title")]
    if nav_row:
        row_children.append(nav_row)
    row = DesignNode(
        type="frame", name="header_row", width=width, height=tokens.component_sizes.nav_height,
        color=tokens.colors.surface,
        auto_layout=AutoLayoutConfig(
            direction="HORIZONTAL", spacing=tokens.spacing.md,
            padding_top=0, padding_bottom=0, padding_left=tokens.spacing.xl, padding_right=tokens.spacing.xl,
            align_items="SPACE_BETWEEN", counter_axis_align="CENTER",
            primary_axis_sizing="FIXED", counter_axis_sizing="FIXED",
        ),
        children=row_children,
    )
    bottom_divider = divider(tokens, name="header_divider")
    return DesignNode(
        type="frame", name=name, width=width, height=tokens.component_sizes.nav_height + 1,
        auto_layout=AutoLayoutConfig(direction="VERTICAL", spacing=0, padding=0, primary_axis_sizing="FIXED", counter_axis_sizing="FIXED"),
        children=[row, bottom_divider], semantic="header",
    )


def sidebar(tokens: DesignTokens, items: list[str], active_index: int = 0, width: int | None = None, height: int = 900, name: str = "sidebar") -> DesignNode:
    w = width or tokens.component_sizes.sidebar_width
    children = []
    for i, item in enumerate(items):
        is_active = i == active_index
        row_children = [
            icon_glyph(tokens, glyph=item[:1].upper(), color=tokens.colors.primary if is_active else tokens.colors.text_secondary, name="nav_icon"),
            DesignNode(
                type="text", name="nav_label", content=item,
                font_size=tokens.typography.body.size, font_weight="Medium" if is_active else "Regular",
                text_color=tokens.colors.primary if is_active else tokens.colors.text_secondary,
                width=140, height=int(tokens.typography.body.size * 1.3), text_auto_resize="WIDTH_AND_HEIGHT",
            ),
        ]
        children.append(DesignNode(
            type="frame", name=f"sidebar_item_{i}", width=w - (2 * tokens.spacing.sm), height=40,
            color=tokens.colors.secondary if is_active else None, corner_radius=tokens.radius.sm,
            auto_layout=AutoLayoutConfig(
                direction="HORIZONTAL", spacing=tokens.spacing.sm, padding_top=8, padding_bottom=8, padding_left=12, padding_right=12,
                align_items="MIN", counter_axis_align="CENTER", primary_axis_sizing="FIXED", counter_axis_sizing="AUTO",
            ),
            children=row_children, semantic="nav_item",
        ))
    return DesignNode(
        type="frame", name=name, width=w, height=height, color=tokens.colors.surface,
        stroke_color=tokens.colors.border, stroke_weight=1.0,
        auto_layout=AutoLayoutConfig(
            direction="VERTICAL", spacing=tokens.spacing.xs, padding=tokens.spacing.sm,
            primary_axis_sizing="FIXED", counter_axis_sizing="FIXED",
        ),
        children=children, semantic="sidebar",
        layout_align="STRETCH",
    )


def modal(tokens: DesignTokens, title: str, body: str, actions: list[str] | None = None, width: int = 420, name: str = "modal") -> DesignNode:
    header_row = DesignNode(
        type="frame", name="modal_header", width=width - 2 * tokens.spacing.lg, height=28,
        auto_layout=AutoLayoutConfig(direction="HORIZONTAL", spacing=0, padding=0, align_items="SPACE_BETWEEN", counter_axis_align="CENTER", primary_axis_sizing="FIXED", counter_axis_sizing="AUTO"),
        children=[heading(tokens, title, level="h3", name="modal_title"), caption_text(tokens, "\u2715", name="modal_close")],
    )
    children = [header_row, paragraph(tokens, body, width=width - 2 * tokens.spacing.lg, name="modal_body")]
    if actions:
        children.append(button_group(tokens, actions, variant="primary", name="modal_actions"))
    return DesignNode(
        type="frame", name=name, width=width, height=200,
        color=tokens.colors.surface, corner_radius=tokens.radius.lg, effects=[tokens.shadow_strong],
        auto_layout=AutoLayoutConfig(direction="VERTICAL", spacing=tokens.spacing.md, padding=tokens.spacing.lg, primary_axis_sizing="AUTO", counter_axis_sizing="FIXED"),
        children=children, semantic="modal",
    )


def section(tokens: DesignTokens, title: str | None, children: list[DesignNode], width: int | None = None, name: str = "section") -> DesignNode:
    node_children = []
    if title:
        node_children.append(heading(tokens, title, level="h2", name="section_title"))
    node_children.extend(children)
    return DesignNode(
        type="frame", name=name, width=width or 600, height=100,
        auto_layout=AutoLayoutConfig(direction="VERTICAL", spacing=tokens.spacing.md, padding=0, primary_axis_sizing="AUTO", counter_axis_sizing="FIXED" if width else "AUTO"),
        children=node_children, semantic="section",
    )


# ---------------------------------------------------------------------------
# Page composition
# ---------------------------------------------------------------------------

def page(
    tokens: DesignTokens,
    screen_name: str,
    platform: str = "desktop",
    header: DesignNode | None = None,
    sidebar_node: DesignNode | None = None,
    content: list[DesignNode] | None = None,
    name: str | None = None,
) -> DesignNode:
    """
    Assemble the standard page skeleton: an optional header stacked on top,
    below it a body row containing an optional sidebar plus a scrollable
    content column -- the exact "nested frames/containers for page, header,
    sidebar, content" structure Phase 4 asks for, built entirely from Auto
    Layout (no manual x/y anywhere in this tree).
    """
    width, height = PLATFORM_SIZES.get(platform, PLATFORM_SIZES["desktop"])
    header_height = header.height if header else 0
    body_height = height - header_height

    content_children = content or []
    content_frame = DesignNode(
        type="frame", name="content", width=width - (sidebar_node.width if sidebar_node else 0), height=body_height,
        color=tokens.colors.background,
        auto_layout=AutoLayoutConfig(
            direction="VERTICAL", spacing=tokens.spacing.xl, padding=tokens.spacing.xl,
            primary_axis_sizing="FIXED", counter_axis_sizing="FIXED",
        ),
        layout_grow=1, layout_align="STRETCH",
        children=content_children, semantic="content",
    )

    body_children = ([sidebar_node] if sidebar_node else []) + [content_frame]
    if sidebar_node is not None:
        sidebar_node.height = body_height

    body_row = DesignNode(
        type="frame", name="body", width=width, height=body_height, color=tokens.colors.background,
        auto_layout=AutoLayoutConfig(direction="HORIZONTAL", spacing=0, padding=0, primary_axis_sizing="FIXED", counter_axis_sizing="FIXED"),
        children=body_children,
    )

    root_children = ([header] if header else []) + [body_row]
    return DesignNode(
        type="frame", name=name or slugify(screen_name), width=width, height=height,
        color=tokens.colors.background,
        auto_layout=AutoLayoutConfig(direction="VERTICAL", spacing=0, padding=0, primary_axis_sizing="FIXED", counter_axis_sizing="FIXED"),
        children=root_children, semantic="page",
    )
