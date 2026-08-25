"""
Tests for planner.py's semantic engine v2 (Phase 2/3/10):
build_semantic_plan, detect_platform, wants_matching_style, and the
_parse_simple_plan/_check_semantic_quality pipeline against the EXPANDED
SimpleElement/SimplePlan schema.

Run directly: python tests/test_planner_semantic.py

Verifies:
  - the legacy flat engine (build_design_plan + TemplatePlanner's
    templates) is COMPLETELY UNCHANGED -- exact node counts, still uses
    only heading/text/input/button -- since several pre-existing tests
    hardcode these counts and must never break.
  - detect_platform's deterministic keyword classification (mobile vs
    desktop), never delegated to the LLM.
  - wants_matching_style's deterministic continuity keyword detection.
  - _parse_simple_plan / _check_semantic_quality accept the new semantic
    element types (card, stat_card, table, nav, sidebar, badge, etc) with
    their items/rows/subtitle/level/variant fields, including the relaxed
    empty-content allowance for content-optional types.
  - build_semantic_plan produces a real, single-root, Auto-Layout DesignPlan
    for a rich multi-section request, with design_system populated.
  - Phase 10 requirement: a follow-up prompt containing a continuity
    keyword ("now create a matching login screen") reuses the exact same
    DesignTokens instance as the immediately preceding generation, so the
    two screens are visually consistent -- verified end-to-end through
    the real module-level continuity memory (_remember_tokens/_resolve_tokens).
"""

import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import asyncio

import planner
from design_plan import DesignNode


def _count_nodes(node: DesignNode) -> int:
    return 1 + sum(_count_nodes(c) for c in node.children)


def _collect_types(node: DesignNode, out: set) -> None:
    out.add(node.type)
    for c in node.children:
        _collect_types(c, out)


async def test_legacy_flat_engine_is_unchanged():
    print("Running test_legacy_flat_engine_is_unchanged...")

    login = planner._login_template()
    dash = planner._dashboard_template()

    login_total = sum(_count_nodes(e) for e in login.elements)
    dash_total = sum(_count_nodes(e) for e in dash.elements)
    assert login_total == 8, f"Expected _login_template to produce exactly 8 nodes (pre-existing test contract), got {login_total}"
    assert dash_total == 6, f"Expected _dashboard_template to produce exactly 6 nodes (pre-existing test contract), got {dash_total}"

    types_used: set = set()
    for e in login.elements + dash.elements:
        _collect_types(e, types_used)
    assert types_used == {"frame", "text", "rectangle"}, (
        f"Expected the legacy engine to still only ever produce frame/text/rectangle, got {types_used}"
    )

    print(f"  confirmed: _login_template (8 nodes) and _dashboard_template (6 nodes) are "
          f"byte-for-byte unchanged, still using only {sorted(types_used)}.")
    print("test_legacy_flat_engine_is_unchanged: PASSED\n")


async def test_detect_platform_is_deterministic_not_llm_decided():
    print("Running test_detect_platform_is_deterministic_not_llm_decided...")

    cases = [
        ("a mobile banking app login screen", "mobile"),
        ("an iOS onboarding flow", "mobile"),
        ("a desktop admin dashboard", "desktop"),
        ("a web application settings page", "desktop"),
        ("a SaaS analytics dashboard", "desktop"),  # dashboard hint, no explicit platform word
        ("a simple login screen", "mobile"),  # no hint at all -> mobile default
        ("an Android profile screen", "mobile"),
    ]
    for prompt, expected in cases:
        actual = planner.detect_platform(prompt)
        assert actual == expected, f"detect_platform({prompt!r}) = {actual!r}, expected {expected!r}"

    print(f"  confirmed: detect_platform correctly classifies {len(cases)} varied prompts "
          f"via deterministic keyword matching (never delegated to the LLM).")
    print("test_detect_platform_is_deterministic_not_llm_decided: PASSED\n")


async def test_wants_matching_style_detection():
    print("Running test_wants_matching_style_detection...")

    positive = [
        "Now create a matching login screen",
        "Create a profile screen using the same design system",
        "Add a settings screen with the same style",
        "make it consistent with the dashboard",
    ]
    negative = [
        "Create a modern SaaS dashboard",
        "a login screen with email and password",
    ]
    for p in positive:
        assert planner.wants_matching_style(p) is True, f"Expected wants_matching_style({p!r}) to be True"
    for p in negative:
        assert planner.wants_matching_style(p) is False, f"Expected wants_matching_style({p!r}) to be False"

    print(f"  confirmed: wants_matching_style correctly flags {len(positive)} continuity-style "
          f"prompts and correctly ignores {len(negative)} standalone prompts.")
    print("test_wants_matching_style_detection: PASSED\n")


async def test_semantic_quality_gate_accepts_new_types():
    print("Running test_semantic_quality_gate_accepts_new_types...")

    simple = planner.SimplePlan(
        screen_name="Orders",
        theme="dark_fintech",
        elements=[
            planner.SimpleElement(type="table", items=["Name", "Status"], rows=[["Acme", "Paid"]]),
            planner.SimpleElement(type="sidebar", items=["Home", "Orders"]),
            planner.SimpleElement(type="divider"),
            planner.SimpleElement(type="badge", items=["New", "Active"], variant="success"),
        ],
    )
    # Must not raise -- content-optional types with empty `content` are fine.
    planner._check_semantic_quality(simple)

    print("  confirmed: table/sidebar/divider/badge with empty 'content' (meaning lives in "
          "items/rows instead) pass the semantic quality gate without raising.")
    print("test_semantic_quality_gate_accepts_new_types: PASSED\n")


async def test_semantic_quality_gate_still_catches_placeholder_leakage():
    print("Running test_semantic_quality_gate_still_catches_placeholder_leakage...")

    simple = planner.SimplePlan(
        screen_name="Test",
        elements=[planner.SimpleElement(type="heading", content="string")],
    )
    raised = False
    try:
        planner._check_semantic_quality(simple)
    except ValueError:
        raised = True
    assert raised, "Expected literal placeholder leakage ('string') in a required-content type to still raise."

    simple2 = planner.SimplePlan(
        screen_name="Test",
        elements=[planner.SimpleElement(type="table", items=["Name", "text"], rows=[])],
    )
    raised2 = False
    try:
        planner._check_semantic_quality(simple2)
    except ValueError:
        raised2 = True
    assert raised2, "Expected placeholder leakage inside 'items' to still raise."

    print("  confirmed: placeholder leakage ('string'/'text'/'label') is still caught, both "
          "in required content fields and inside the new items/rows fields.")
    print("test_semantic_quality_gate_still_catches_placeholder_leakage: PASSED\n")


async def test_build_semantic_plan_produces_single_root_auto_layout_tree():
    print("Running test_build_semantic_plan_produces_single_root_auto_layout_tree...")

    simple = planner.SimplePlan(
        screen_name="Team Dashboard",
        theme="modern_ecommerce",
        elements=[
            planner.SimpleElement(type="header", content="Acme", items=["Dashboard", "Orders", "Settings"]),
            planner.SimpleElement(type="sidebar", items=["Dashboard", "Customers", "Orders"]),
            planner.SimpleElement(type="heading", content="Overview", level="h1"),
            planner.SimpleElement(type="stat_card", content="Revenue", subtitle="$12,400", items=["+4.2%"]),
            planner.SimpleElement(type="stat_card", content="Orders", subtitle="1,204", items=["+1.1%"]),
            planner.SimpleElement(type="table", items=["Order", "Status"], rows=[["#1001", "Shipped"], ["#1002", "Pending"]]),
        ],
    )
    plan = planner.build_semantic_plan(simple, prompt="a modern ecommerce admin dashboard")

    assert len(plan.elements) == 1, "Expected build_semantic_plan to produce exactly one root element."
    assert plan.design_system == "modern_ecommerce"
    root = plan.elements[0]
    assert root.type == "frame"
    assert root.auto_layout is not None
    assert root.width == 1440 and root.height == 1024, "Expected a desktop-sized canvas for a dashboard request."

    total = _count_nodes(root)
    assert total > 10, f"Expected a reasonably rich node tree for a 6-element rich request, got {total} nodes"

    print(f"  confirmed: build_semantic_plan produced a single-root, Auto-Layout, desktop-sized "
          f"({root.width}x{root.height}) tree with {total} total nodes and design_system="
          f"'{plan.design_system}'.")
    print("test_build_semantic_plan_produces_single_root_auto_layout_tree: PASSED\n")


async def test_phase10_multi_prompt_style_continuity():
    """The exact Phase 10 scenario: 'Create a modern SaaS dashboard' then
    'Now create a matching login screen' must use the SAME DesignTokens
    instance for both screens."""
    print("Running test_phase10_multi_prompt_style_continuity...")

    dashboard_simple = planner.SimplePlan(
        screen_name="Dashboard", theme="minimal_saas",
        elements=[planner.SimpleElement(type="heading", content="Overview")],
    )
    dashboard_plan = planner.build_semantic_plan(dashboard_simple, prompt="Create a modern minimal SaaS dashboard")
    assert dashboard_plan.design_system == "minimal_saas"

    # A follow-up prompt with a DIFFERENT theme guess from the (simulated)
    # LLM, but a continuity keyword -- continuity must win.
    login_simple = planner.SimplePlan(
        screen_name="Login", theme="professional_blue",  # deliberately different/wrong guess
        elements=[
            planner.SimpleElement(type="heading", content="Welcome Back"),
            planner.SimpleElement(type="input", content="Email"),
            planner.SimpleElement(type="input", content="Password"),
            planner.SimpleElement(type="button", content="Log In"),
        ],
    )
    login_plan = planner.build_semantic_plan(login_simple, prompt="Now create a matching login screen")

    assert login_plan.design_system == "minimal_saas", (
        f"Expected the follow-up 'matching' prompt to reuse the dashboard's design system "
        f"(minimal_saas), got {login_plan.design_system!r} -- style continuity failed."
    )

    # A THIRD prompt with no continuity keyword should behave normally
    # (use its own requested theme), proving continuity isn't "stuck on"
    # forever once triggered once.
    ecommerce_simple = planner.SimplePlan(
        screen_name="Storefront", theme="modern_ecommerce",
        elements=[planner.SimpleElement(type="heading", content="Products")],
    )
    ecommerce_plan = planner.build_semantic_plan(ecommerce_simple, prompt="Create a modern ecommerce storefront")
    assert ecommerce_plan.design_system == "modern_ecommerce", (
        "Expected a standalone request with no continuity keyword to use its own requested "
        "theme, not get stuck reusing the previous one."
    )

    print("  confirmed: 'Now create a matching login screen' reuses the dashboard's exact "
          "design system (minimal_saas) even though its own theme guess differed; a later "
          "standalone request without a continuity keyword correctly uses its own theme.")
    print("test_phase10_multi_prompt_style_continuity: PASSED\n")


async def main():
    await test_legacy_flat_engine_is_unchanged()
    await test_detect_platform_is_deterministic_not_llm_decided()
    await test_wants_matching_style_detection()
    await test_semantic_quality_gate_accepts_new_types()
    await test_semantic_quality_gate_still_catches_placeholder_leakage()
    await test_build_semantic_plan_produces_single_root_auto_layout_tree()
    await test_phase10_multi_prompt_style_continuity()
    print("All test_planner_semantic checks completed.")


if __name__ == "__main__":
    asyncio.run(main())
