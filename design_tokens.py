"""
Design tokens — a coherent, named set of colors, typography, spacing,
radius, shadows, and component sizes that every node in a generated
screen draws from.

This replaces "random colors and font sizes per node" with a single
source of truth per screen: every composer function in components.py
takes a DesignTokens instance and reads from it, so a button's corner
radius, a card's padding, and a heading's weight are always internally
consistent, and swapping the DesignTokens instance changes the whole
screen's look without touching any layout logic.

Backward compatibility note: the original 4 curated palettes in
planner.py (`professional_blue`, `midnight_premium`, `calm_wellness`,
`warm_friendly`) only defined 7 raw colors (bg/heading/text/label/
input_bg/button_bg/button_text) with no typography/spacing/radius/shadow
scale. Those 4 names are preserved here (so existing theme selection by
name keeps working), now backed by a full token set instead of 7 loose
values. Four additional presets are added for explicitly requested
styles ("minimal SaaS dashboard", "dark fintech dashboard", "modern
ecommerce application", "healthcare mobile application") so different
prompts produce visibly different, but each internally coherent,
results.
"""

from __future__ import annotations
from typing import Literal

from pydantic import BaseModel, Field

from design_plan import ColorRGB, EffectConfig


def _c(r: float, g: float, b: float) -> ColorRGB:
    return ColorRGB(r=r, g=g, b=b)


class ColorTokens(BaseModel):
    primary: ColorRGB
    primary_hover: ColorRGB
    on_primary: ColorRGB           # text/icon color placed on top of `primary`
    secondary: ColorRGB
    on_secondary: ColorRGB
    background: ColorRGB           # page background
    surface: ColorRGB              # card/panel background, sits on `background`
    surface_alt: ColorRGB          # input fields, table stripes, subtle fills
    border: ColorRGB
    text_primary: ColorRGB
    text_secondary: ColorRGB
    text_disabled: ColorRGB
    success: ColorRGB
    warning: ColorRGB
    error: ColorRGB


class TypeStyle(BaseModel):
    size: int
    weight: Literal["Regular", "Medium", "Bold"] = "Regular"
    # Informational only (Figma text nodes auto-size their own line height);
    # kept so a future renderer/inspector can reason about vertical rhythm.
    line_height_multiplier: float = 1.3


class TypographyTokens(BaseModel):
    display: TypeStyle
    h1: TypeStyle
    h2: TypeStyle
    h3: TypeStyle
    body: TypeStyle
    small: TypeStyle
    caption: TypeStyle
    label: TypeStyle
    button: TypeStyle


class SpacingTokens(BaseModel):
    xs: int = 4
    sm: int = 8
    md: int = 16
    lg: int = 24
    xl: int = 32
    xxl: int = 48


class RadiusTokens(BaseModel):
    sm: int = 6
    md: int = 10
    lg: int = 16
    full: int = 999


class ComponentSizeTokens(BaseModel):
    button_height: int = 44
    input_height: int = 44
    nav_height: int = 64
    sidebar_width: int = 240
    avatar_size: int = 36
    icon_size: int = 20
    card_padding: int = 20


class DesignTokens(BaseModel):
    name: str
    colors: ColorTokens
    typography: TypographyTokens
    spacing: SpacingTokens = Field(default_factory=SpacingTokens)
    radius: RadiusTokens = Field(default_factory=RadiusTokens)
    shadow_soft: EffectConfig
    shadow_strong: EffectConfig
    component_sizes: ComponentSizeTokens = Field(default_factory=ComponentSizeTokens)


def _shadows(opacity_soft: float, opacity_strong: float, dark: bool = False) -> tuple[EffectConfig, EffectConfig]:
    shadow_color = _c(0.0, 0.0, 0.0) if not dark else _c(0.0, 0.0, 0.0)
    soft = EffectConfig(type="DROP_SHADOW", color=shadow_color, radius=8, offset_x=0, offset_y=2, opacity=opacity_soft)
    strong = EffectConfig(type="DROP_SHADOW", color=shadow_color, radius=20, offset_x=0, offset_y=8, opacity=opacity_strong)
    return soft, strong


def _typography(display=44, h1=32, h2=24, h3=19, body=15, small=13, caption=12, label=13, button=15) -> TypographyTokens:
    return TypographyTokens(
        display=TypeStyle(size=display, weight="Bold"),
        h1=TypeStyle(size=h1, weight="Bold"),
        h2=TypeStyle(size=h2, weight="Bold"),
        h3=TypeStyle(size=h3, weight="Medium"),
        body=TypeStyle(size=body, weight="Regular"),
        small=TypeStyle(size=small, weight="Regular"),
        caption=TypeStyle(size=caption, weight="Regular"),
        label=TypeStyle(size=label, weight="Medium"),
        button=TypeStyle(size=button, weight="Medium"),
    )


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------

def _professional_blue() -> DesignTokens:
    shadow_soft, shadow_strong = _shadows(0.06, 0.14)
    return DesignTokens(
        name="professional_blue",
        colors=ColorTokens(
            primary=_c(0.15, 0.4, 0.95), primary_hover=_c(0.12, 0.33, 0.85), on_primary=_c(1, 1, 1),
            secondary=_c(0.93, 0.95, 0.99), on_secondary=_c(0.15, 0.4, 0.95),
            background=_c(1, 1, 1), surface=_c(1, 1, 1), surface_alt=_c(0.95, 0.96, 0.98),
            border=_c(0.88, 0.9, 0.93),
            text_primary=_c(0.07, 0.09, 0.15), text_secondary=_c(0.35, 0.38, 0.45), text_disabled=_c(0.65, 0.67, 0.72),
            success=_c(0.13, 0.62, 0.4), warning=_c(0.85, 0.55, 0.1), error=_c(0.85, 0.2, 0.2),
        ),
        typography=_typography(),
        shadow_soft=shadow_soft, shadow_strong=shadow_strong,
    )


def _midnight_premium() -> DesignTokens:
    shadow_soft, shadow_strong = _shadows(0.35, 0.5, dark=True)
    return DesignTokens(
        name="midnight_premium",
        colors=ColorTokens(
            primary=_c(0.35, 0.65, 1.0), primary_hover=_c(0.45, 0.72, 1.0), on_primary=_c(0.05, 0.06, 0.08),
            secondary=_c(0.18, 0.19, 0.23), on_secondary=_c(0.85, 0.87, 0.92),
            background=_c(0.07, 0.08, 0.10), surface=_c(0.11, 0.12, 0.15), surface_alt=_c(0.15, 0.16, 0.19),
            border=_c(0.22, 0.23, 0.27),
            text_primary=_c(0.97, 0.97, 0.98), text_secondary=_c(0.75, 0.76, 0.8), text_disabled=_c(0.45, 0.46, 0.5),
            success=_c(0.3, 0.8, 0.55), warning=_c(0.95, 0.7, 0.25), error=_c(0.95, 0.4, 0.4),
        ),
        typography=_typography(),
        shadow_soft=shadow_soft, shadow_strong=shadow_strong,
    )


def _calm_wellness() -> DesignTokens:
    shadow_soft, shadow_strong = _shadows(0.05, 0.12)
    return DesignTokens(
        name="calm_wellness",
        colors=ColorTokens(
            primary=_c(0.35, 0.55, 0.45), primary_hover=_c(0.28, 0.46, 0.37), on_primary=_c(1, 1, 1),
            secondary=_c(0.9, 0.94, 0.9), on_secondary=_c(0.2, 0.35, 0.27),
            background=_c(0.98, 0.97, 0.95), surface=_c(1, 1, 1), surface_alt=_c(0.93, 0.95, 0.92),
            border=_c(0.87, 0.89, 0.85),
            text_primary=_c(0.13, 0.24, 0.18), text_secondary=_c(0.35, 0.42, 0.38), text_disabled=_c(0.62, 0.66, 0.6),
            success=_c(0.3, 0.6, 0.4), warning=_c(0.8, 0.6, 0.2), error=_c(0.78, 0.32, 0.28),
        ),
        typography=_typography(h1=28, h2=22),
        shadow_soft=shadow_soft, shadow_strong=shadow_strong,
        radius=RadiusTokens(sm=10, md=16, lg=22, full=999),
    )


def _warm_friendly() -> DesignTokens:
    shadow_soft, shadow_strong = _shadows(0.08, 0.16)
    return DesignTokens(
        name="warm_friendly",
        colors=ColorTokens(
            primary=_c(0.93, 0.5, 0.2), primary_hover=_c(0.85, 0.42, 0.14), on_primary=_c(1, 1, 1),
            secondary=_c(0.98, 0.92, 0.85), on_secondary=_c(0.55, 0.3, 0.1),
            background=_c(0.99, 0.97, 0.93), surface=_c(1, 1, 1), surface_alt=_c(0.96, 0.92, 0.86),
            border=_c(0.9, 0.85, 0.76),
            text_primary=_c(0.28, 0.17, 0.08), text_secondary=_c(0.45, 0.36, 0.28), text_disabled=_c(0.68, 0.6, 0.52),
            success=_c(0.35, 0.6, 0.3), warning=_c(0.88, 0.6, 0.15), error=_c(0.8, 0.3, 0.22),
        ),
        typography=_typography(),
        shadow_soft=shadow_soft, shadow_strong=shadow_strong,
        radius=RadiusTokens(sm=8, md=14, lg=20, full=999),
    )


def _minimal_saas() -> DesignTokens:
    """Near-monochrome, single indigo accent, generous whitespace, tight
    sharp corners -- the "minimal SaaS dashboard" look."""
    shadow_soft, shadow_strong = _shadows(0.04, 0.10)
    return DesignTokens(
        name="minimal_saas",
        colors=ColorTokens(
            primary=_c(0.31, 0.27, 0.9), primary_hover=_c(0.26, 0.22, 0.8), on_primary=_c(1, 1, 1),
            secondary=_c(0.95, 0.95, 0.97), on_secondary=_c(0.31, 0.27, 0.9),
            background=_c(0.99, 0.99, 0.99), surface=_c(1, 1, 1), surface_alt=_c(0.96, 0.96, 0.97),
            border=_c(0.9, 0.9, 0.92),
            text_primary=_c(0.1, 0.1, 0.13), text_secondary=_c(0.45, 0.45, 0.5), text_disabled=_c(0.7, 0.7, 0.74),
            success=_c(0.16, 0.6, 0.4), warning=_c(0.8, 0.55, 0.05), error=_c(0.82, 0.24, 0.24),
        ),
        typography=_typography(display=40, h1=28, h2=20, h3=16, body=14, small=12, caption=11, label=12, button=14),
        shadow_soft=shadow_soft, shadow_strong=shadow_strong,
        spacing=SpacingTokens(xs=4, sm=8, md=16, lg=28, xl=40, xxl=56),
        radius=RadiusTokens(sm=4, md=6, lg=10, full=999),
        component_sizes=ComponentSizeTokens(button_height=40, input_height=40, nav_height=60, sidebar_width=220, avatar_size=32, icon_size=18, card_padding=24),
    )


def _dark_fintech() -> DesignTokens:
    """Near-black, high-contrast neon green/teal accent, dense data-heavy
    sizing -- the "dark fintech dashboard" look."""
    shadow_soft, shadow_strong = _shadows(0.45, 0.6, dark=True)
    return DesignTokens(
        name="dark_fintech",
        colors=ColorTokens(
            primary=_c(0.15, 0.95, 0.6), primary_hover=_c(0.1, 0.85, 0.52), on_primary=_c(0.02, 0.05, 0.04),
            secondary=_c(0.14, 0.16, 0.2), on_secondary=_c(0.8, 0.85, 0.82),
            background=_c(0.04, 0.045, 0.06), surface=_c(0.08, 0.09, 0.11), surface_alt=_c(0.12, 0.13, 0.16),
            border=_c(0.2, 0.21, 0.25),
            text_primary=_c(0.95, 0.97, 0.96), text_secondary=_c(0.65, 0.68, 0.67), text_disabled=_c(0.4, 0.42, 0.42),
            success=_c(0.15, 0.95, 0.6), warning=_c(0.98, 0.75, 0.2), error=_c(0.98, 0.35, 0.4),
        ),
        typography=_typography(display=40, h1=26, h2=19, h3=15, body=13, small=12, caption=11, label=11, button=13),
        shadow_soft=shadow_soft, shadow_strong=shadow_strong,
        spacing=SpacingTokens(xs=4, sm=8, md=14, lg=20, xl=28, xxl=40),
        radius=RadiusTokens(sm=4, md=8, lg=12, full=999),
        component_sizes=ComponentSizeTokens(button_height=40, input_height=40, nav_height=56, sidebar_width=220, avatar_size=32, icon_size=16, card_padding=16),
    )


def _modern_ecommerce() -> DesignTokens:
    """Bold vibrant accent, larger radius, punchy typography -- the "modern
    ecommerce application" look."""
    shadow_soft, shadow_strong = _shadows(0.08, 0.18)
    return DesignTokens(
        name="modern_ecommerce",
        colors=ColorTokens(
            primary=_c(0.86, 0.16, 0.42), primary_hover=_c(0.75, 0.12, 0.36), on_primary=_c(1, 1, 1),
            secondary=_c(0.99, 0.93, 0.96), on_secondary=_c(0.86, 0.16, 0.42),
            background=_c(1, 1, 1), surface=_c(1, 1, 1), surface_alt=_c(0.97, 0.96, 0.97),
            border=_c(0.9, 0.88, 0.9),
            text_primary=_c(0.1, 0.08, 0.1), text_secondary=_c(0.4, 0.37, 0.4), text_disabled=_c(0.68, 0.65, 0.68),
            success=_c(0.13, 0.62, 0.4), warning=_c(0.9, 0.6, 0.1), error=_c(0.85, 0.2, 0.3),
        ),
        typography=_typography(display=46, h1=30, h2=22, h3=18, body=15, small=13, caption=12, label=13, button=16),
        shadow_soft=shadow_soft, shadow_strong=shadow_strong,
        radius=RadiusTokens(sm=10, md=16, lg=24, full=999),
        component_sizes=ComponentSizeTokens(button_height=48, input_height=46, nav_height=68, sidebar_width=240, avatar_size=40, icon_size=22, card_padding=18),
    )


def _healthcare_mobile() -> DesignTokens:
    """Soft blue/teal, high trust and calm, larger touch targets for
    accessibility -- the "healthcare mobile application" look."""
    shadow_soft, shadow_strong = _shadows(0.05, 0.12)
    return DesignTokens(
        name="healthcare_mobile",
        colors=ColorTokens(
            primary=_c(0.2, 0.5, 0.62), primary_hover=_c(0.16, 0.42, 0.53), on_primary=_c(1, 1, 1),
            secondary=_c(0.9, 0.96, 0.97), on_secondary=_c(0.2, 0.5, 0.62),
            background=_c(0.98, 0.99, 0.99), surface=_c(1, 1, 1), surface_alt=_c(0.94, 0.97, 0.97),
            border=_c(0.87, 0.91, 0.91),
            text_primary=_c(0.1, 0.16, 0.18), text_secondary=_c(0.38, 0.45, 0.47), text_disabled=_c(0.65, 0.7, 0.71),
            success=_c(0.2, 0.6, 0.42), warning=_c(0.85, 0.6, 0.15), error=_c(0.8, 0.28, 0.28),
        ),
        typography=_typography(display=36, h1=26, h2=21, h3=18, body=16, small=14, caption=13, label=14, button=17),
        shadow_soft=shadow_soft, shadow_strong=shadow_strong,
        radius=RadiusTokens(sm=10, md=14, lg=20, full=999),
        component_sizes=ComponentSizeTokens(button_height=52, input_height=52, nav_height=64, sidebar_width=240, avatar_size=40, icon_size=24, card_padding=20),
    )


_PRESETS: dict[str, DesignTokens] = {
    "professional_blue": _professional_blue(),
    "midnight_premium": _midnight_premium(),
    "calm_wellness": _calm_wellness(),
    "warm_friendly": _warm_friendly(),
    "minimal_saas": _minimal_saas(),
    "dark_fintech": _dark_fintech(),
    "modern_ecommerce": _modern_ecommerce(),
    "healthcare_mobile": _healthcare_mobile(),
}

DEFAULT_STYLE = "professional_blue"

STYLE_NAMES: list[str] = list(_PRESETS.keys())


def get_tokens(style: str) -> DesignTokens:
    """Look up a named preset, falling back to DEFAULT_STYLE for an
    unrecognized name instead of raising -- keeps the caller (planner.py,
    which gets this name from an LLM) tolerant of a slightly-off model
    output the same way the original `_THEMES.get(..., _THEMES[_DEFAULT_THEME])`
    fallback did."""
    return _PRESETS.get(style, _PRESETS[DEFAULT_STYLE])
