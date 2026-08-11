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
    align_items: Literal["MIN", "CENTER", "MAX", "SPACE_BETWEEN"] = "MIN"


NodeType = Literal[
    "frame", "text", "rectangle", "ellipse", "line",
    "image_placeholder", "icon", "component", "component_set", "group",
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

    content: str = ""            # text / icon label / placeholder caption
    font_size: int = 16
    corner_radius: int = 0
    color: ColorRGB | None = None
    text_color: ColorRGB | None = None

    auto_layout: AutoLayoutConfig | None = None
    constraints: ConstraintConfig | None = None
    effects: list[EffectConfig] = Field(default_factory=list)

    variant_properties: dict[str, str] = Field(default_factory=dict)
    children: list["DesignNode"] = Field(default_factory=list)


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
    elements: list[DesignNode]