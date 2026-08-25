"""
Design Plan schema — the structured intermediate representation between
design intent (LLM or template) and Figma commands.

Supports nesting (frames/groups/components with children), auto layout,
effects, constraints, and design tokens (color/text styles, variables).
"""

from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, Field


class ColorRGB(BaseModel):
    r: float = Field(ge=0.0, le=1.0)
    g: float = Field(ge=0.0, le=1.0)
    b: float = Field(ge=0.0, le=1.0)


class EffectConfig(BaseModel):
    type: Literal["DROP_SHADOW", "INNER_SHADOW", "LAYER_BLUR", "BACKGROUND_BLUR"]
    color: ColorRGB | None = None
    radius: float = 4
    offset_x: float = 0
    offset_y: float = 2
    opacity: float = 0.25


class ConstraintConfig(BaseModel):
    horizontal: Literal["MIN", "MAX", "CENTER", "STRETCH", "SCALE"] = "MIN"
    vertical: Literal["MIN", "MAX", "CENTER", "STRETCH", "SCALE"] = "MIN"


class AutoLayoutConfig(BaseModel):
    direction: Literal["HORIZONTAL", "VERTICAL"] = "VERTICAL"
    spacing: int = 8
    padding: int = 16
    # Per-side padding overrides. None means "fall back to the uniform
    # `padding` value above" -- additive, so any existing/hand-built plan
    # that only sets `padding` behaves identically to before.
    padding_top: int | None = None
    padding_right: int | None = None
    padding_bottom: int | None = None
    padding_left: int | None = None
    align_items: Literal["MIN", "CENTER", "MAX", "SPACE_BETWEEN"] = "MIN"
    # Cross-axis alignment (Figma's counterAxisAlignItems) -- e.g. vertically
    # centering items in a horizontal header row. Not in the original
    # schema; defaults to CENTER because that is what the vast majority of
    # composite UI rows (headers, nav rows, button internals) want, and is a
    # genuine improvement over Figma's own raw MIN default for a brand new
    # auto-layout frame.
    counter_axis_align: Literal["MIN", "CENTER", "MAX"] = "CENTER"
    # Sizing modes (Figma's primaryAxisSizingMode / counterAxisSizingMode).
    # AUTO = "hug contents" on that axis, FIXED = respect the node's own
    # width/height. Defaults (primary=AUTO, counter=FIXED) match the most
    # common case: a vertical stack that grows to fit its content but has
    # an explicit width, e.g. a card or page body.
    primary_axis_sizing: Literal["FIXED", "AUTO"] = "AUTO"
    counter_axis_sizing: Literal["FIXED", "AUTO"] = "FIXED"


NodeType = Literal[
    "frame", "text", "rectangle", "ellipse", "line",
    "image_placeholder", "icon", "component", "component_set", "group",
    # Additive: an instance of a component created earlier in the SAME plan
    # execution (see DesignNode.component_ref / register_as below), enabling
    # real Figma component reuse for repeated atoms (nav items, list items,
    # tabs, badges) instead of duplicating a full node tree for each repeat.
    # Any existing plan that never uses this type is completely unaffected.
    "instance",
]


class DesignNode(BaseModel):
    """
    One node in the design tree. Container types (frame, component, group,
    component_set) use `children`. `variant_properties` is only meaningful
    on direct children of a `component_set` node.
    """

    type: NodeType
    name: str = ""
    x: int = 0
    y: int = 0
    width: int = 100
    height: int = 40

    content: str = Field(default="", max_length=2000)  # text / icon label / placeholder caption
    font_size: int = 16
    # Additive: text weight. Previously every text node was implicitly
    # "Regular" -- this lets headings/labels/buttons render at a real,
    # distinct weight instead of only varying by size.
    font_weight: Literal["Regular", "Medium", "Bold"] = "Regular"
    # Additive: explicit Figma TextNode.textAutoResize mode for `text`
    # nodes. None preserves the ORIGINAL plugin heuristic exactly (see
    # code.js create_text: any named node other than "button_label" with a
    # width got textAutoResize="HEIGHT"; "button_label" and width-less
    # nodes were left at the engine default) -- so any plan built before
    # this field existed renders identically. New semantic composers
    # (components.py) always set this explicitly instead of relying on
    # the name-based heuristic.
    text_auto_resize: Literal["NONE", "WIDTH_AND_HEIGHT", "HEIGHT", "TRUNCATE"] | None = None
    corner_radius: int = 0
    color: ColorRGB | None = None
    text_color: ColorRGB | None = None
    # Additive: node opacity (0-1). Defaults to fully opaque, so any
    # existing node is rendered exactly as before.
    opacity: float = Field(default=1.0, ge=0.0, le=1.0)
    # Additive: stroke/border support. No primitive previously had any
    # border capability at all.
    stroke_color: ColorRGB | None = None
    stroke_weight: float = 1.0

    auto_layout: AutoLayoutConfig | None = None
    # Additive: how THIS node behaves as a child of an auto-layout parent
    # (Figma's layoutAlign / layoutGrow). None/0 means "no opinion", which
    # is a no-op identical to today's behavior.
    layout_align: Literal["MIN", "CENTER", "MAX", "STRETCH"] | None = None
    layout_grow: float = 0
    constraints: ConstraintConfig | None = None
    effects: list[EffectConfig] = Field(default_factory=list)

    # Additive, purely descriptive metadata (e.g. "card", "nav_item",
    # "button"). Never required by the plugin to render correctly -- it
    # exists so semantic intent survives into validation/audit/debugging
    # without changing NodeType or the plugin's dispatch table.
    semantic: str | None = None

    # Additive: component-reuse pair. Set `register_as` on a `component`
    # node to expose it under a logical name for later `instance` nodes IN
    # THE SAME PLAN; set `component_ref` on an `instance` node to point at
    # that name. Resolved by plan_executor at runtime (mirrors the existing
    # created_node_ids allowlist pattern: per-execution, not global).
    register_as: str | None = None
    component_ref: str | None = None

    variant_properties: dict[str, str] = Field(default_factory=dict)
    # Cap enforced at construction time (not just a prompt instruction) to
    # bound plan size/time -- see H-6 in bridge-security-hardening.
    # UNCHANGED from the original cap.
    children: list["DesignNode"] = Field(default_factory=list, max_length=50)


DesignNode.model_rebuild()  # required for the recursive self-reference


class ColorStyleDef(BaseModel):
    name: str
    color: ColorRGB


class TextStyleDef(BaseModel):
    name: str
    font_size: int = 16
    font_weight: Literal["Regular", "Medium", "Bold"] = "Regular"


class VariableDef(BaseModel):
    name: str
    collection: str = "Design Tokens"
    var_type: Literal["COLOR", "FLOAT", "STRING"] = "COLOR"
    color_value: ColorRGB | None = None
    number_value: float | None = None
    string_value: str | None = None


class DesignPlan(BaseModel):
    screen_name: str
    color_styles: list[ColorStyleDef] = Field(default_factory=list)
    text_styles: list[TextStyleDef] = Field(default_factory=list)
    variables: list[VariableDef] = Field(default_factory=list)
    # Cap enforced at construction time -- see H-6 in bridge-security-hardening.
    # UNCHANGED from the original cap.
    elements: list[DesignNode] = Field(max_length=20)

    # Additive: the name of the design-token style (see design_tokens.py)
    # used to generate this plan, if any. Populated by planner.py so a
    # later prompt in the same conversation ("now build a matching login
    # screen") can look it up and reuse the exact same token set instead
    # of re-deriving a style from scratch. None for any plan that predates
    # or doesn't go through the token system (e.g. a hand-built DesignPlan
    # passed straight to generate_screen) -- fully optional.
    design_system: str | None = None

    # Additive: human-readable notes from plan_validation.py describing any
    # automatic corrections made (e.g. "clamped width of 'sidebar' from
    # 4000 to 1200", "renamed duplicate node name 'card' -> 'card_2'").
    # Always populated (possibly empty) by execute_plan; never silently
    # drops information the way validation could if it just "fixed things"
    # with no record.
    validation_notes: list[str] = Field(default_factory=list)