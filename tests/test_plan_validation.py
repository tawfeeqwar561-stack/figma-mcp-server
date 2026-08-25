"""
Tests for plan_validation.py (Phase 9: DesignPlan validation/normalization).

Run directly: python tests/test_plan_validation.py

Verifies each of the task's explicitly listed validation categories:
  - invalid dimensions (clamped, not silently dropped -- reported)
  - duplicate names where problematic (top-level only, by design)
  - invalid Auto Layout (set on a non-auto-layout-capable type)
  - excessive nodes (rejected, not silently truncated)
  - overlapping major containers (root elements nudged apart)
  - missing content (semantic text nodes get a visible placeholder)
  - invalid parent relationships / dangling instance->component refs
  - a completely valid, realistic plan passes through with ZERO notes
    (never "corrects" something that wasn't broken)
  - validate_and_normalize never mutates the caller's original plan object
    (notes describe the change, but the original reference is untouched)
"""

import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import asyncio

import components
import plan_validation as pv
import planner
from design_plan import AutoLayoutConfig, ColorRGB, DesignNode, DesignPlan
from design_tokens import get_tokens


async def test_clamps_invalid_dimensions_and_reports_it():
    print("Running test_clamps_invalid_dimensions_and_reports_it...")

    bad = DesignNode(type="rectangle", name="oversized", width=99999, height=-5, x=10**9, y=-(10**9))
    plan = DesignPlan(screen_name="dim test", elements=[bad])

    fixed, notes = pv.validate_and_normalize(plan)
    fixed_node = fixed.elements[0]

    assert 1 <= fixed_node.width <= pv.MAX_DIMENSION
    assert 1 <= fixed_node.height <= pv.MAX_DIMENSION
    assert abs(fixed_node.x) <= pv._MAX_COORDINATE
    assert abs(fixed_node.y) <= pv._MAX_COORDINATE
    assert any("width" in n and "oversized" in n for n in notes)
    assert any("height" in n and "oversized" in n for n in notes)
    # Original object must be untouched.
    assert bad.width == 99999 and bad.height == -5

    print(f"  confirmed: out-of-range width/height/x/y are clamped into safe bounds and "
          f"reported ({len(notes)} notes), original node object left untouched.")
    print("test_clamps_invalid_dimensions_and_reports_it: PASSED\n")


async def test_deduplicates_top_level_names_only():
    print("Running test_deduplicates_top_level_names_only...")

    a = DesignNode(type="frame", name="screen", width=100, height=100)
    b = DesignNode(type="frame", name="screen", width=100, height=100, x=5000)  # far away, no overlap
    plan = DesignPlan(screen_name="dup test", elements=[a, b])

    fixed, notes = pv.validate_and_normalize(plan)
    names = [n.name for n in fixed.elements]
    assert len(set(names)) == 2, f"Expected unique top-level names after normalization, got {names}"
    assert any("duplicate" in n.lower() for n in notes)

    # Deep, repeated sibling names (e.g. many "cell" text nodes in a table)
    # must NOT be touched -- that repetition is normal, not a bug.
    tokens = get_tokens("professional_blue")
    table = components.table(tokens, ["A", "B"], [["1", "2"], ["3", "4"]])
    plan2 = DesignPlan(screen_name="table test", elements=[table])
    _fixed2, notes2 = pv.validate_and_normalize(plan2)
    assert not any("duplicate" in n.lower() for n in notes2), (
        f"Expected no duplicate-name corrections for legitimately-repeated deep node names, got notes: {notes2}"
    )

    print("  confirmed: duplicate TOP-LEVEL element names are renamed and reported; "
          "legitimately-repeated deep sibling names (e.g. table cells) are left alone.")
    print("test_deduplicates_top_level_names_only: PASSED\n")


async def test_strips_auto_layout_from_non_capable_types():
    print("Running test_strips_auto_layout_from_non_capable_types...")

    bad = DesignNode(type="rectangle", name="rect_with_layout", width=10, height=10, auto_layout=AutoLayoutConfig())
    plan = DesignPlan(screen_name="layout test", elements=[bad])

    fixed, notes = pv.validate_and_normalize(plan)
    assert fixed.elements[0].auto_layout is None
    assert any("auto_layout" in n and "rect_with_layout" in n for n in notes)

    # A frame/component keeping its own auto_layout must be untouched.
    good = DesignNode(type="frame", name="good_frame", width=10, height=10, auto_layout=AutoLayoutConfig())
    plan2 = DesignPlan(screen_name="layout ok test", elements=[good])
    fixed2, notes2 = pv.validate_and_normalize(plan2)
    assert fixed2.elements[0].auto_layout is not None
    assert notes2 == []

    print("  confirmed: auto_layout on a non-capable type (rectangle) is stripped and "
          "reported; a frame's own auto_layout is left untouched.")
    print("test_strips_auto_layout_from_non_capable_types: PASSED\n")


async def test_rejects_excessively_large_plans():
    print("Running test_rejects_excessively_large_plans...")

    big_elements = [
        DesignNode(type="frame", name=f"f{i}", children=[DesignNode(type="rectangle", name=f"r{i}_{j}") for j in range(45)])
        for i in range(10)
    ]
    big_plan = DesignPlan(screen_name="huge", elements=big_elements)

    raised = False
    try:
        pv.validate_and_normalize(big_plan)
    except pv.PlanTooLargeError as exc:
        raised = True
        assert "exceeding the maximum" in str(exc)

    assert raised, "Expected an oversized plan to be REJECTED (raise), never silently truncated."

    print("  confirmed: a plan whose flattened node count exceeds the cap is rejected "
          "with a clear error, never silently truncated (never destroys intent silently).")
    print("test_rejects_excessively_large_plans: PASSED\n")


async def test_resolves_overlapping_root_elements():
    print("Running test_resolves_overlapping_root_elements...")

    a = DesignNode(type="frame", name="screen_a", x=0, y=0, width=375, height=800)
    b = DesignNode(type="frame", name="screen_b", x=0, y=0, width=375, height=800)  # same position -> overlap
    plan = DesignPlan(screen_name="overlap test", elements=[a, b])

    fixed, notes = pv.validate_and_normalize(plan)
    fa, fb = fixed.elements
    overlap = fa.x < fb.x + fb.width and fa.x + fa.width > fb.x and fa.y < fb.y + fb.height and fa.y + fa.height > fb.y
    assert not overlap, f"Expected root elements to no longer overlap after normalization: {fa.x},{fa.width} vs {fb.x},{fb.width}"
    assert any("overlapped" in n for n in notes)

    print(f"  confirmed: overlapping root elements are nudged apart (screen_b.x moved to "
          f"{fb.x}) and the correction is reported.")
    print("test_resolves_overlapping_root_elements: PASSED\n")


async def test_fills_missing_semantic_text_content():
    print("Running test_fills_missing_semantic_text_content...")

    empty_heading = DesignNode(type="text", name="empty_heading", content="", semantic="heading")
    plan = DesignPlan(screen_name="missing content test", elements=[empty_heading])

    fixed, notes = pv.validate_and_normalize(plan)
    assert fixed.elements[0].content.strip() != "", "Expected empty semantic heading content to be filled with a visible placeholder."
    assert any("empty content" in n.lower() for n in notes)

    print("  confirmed: an empty heading/paragraph/label/caption text node is filled with "
          "a visible placeholder and reported, instead of rendering as a blank node.")
    print("test_fills_missing_semantic_text_content: PASSED\n")


async def test_dangling_instance_ref_becomes_placeholder_frame():
    print("Running test_dangling_instance_ref_becomes_placeholder_frame...")

    orphan = DesignNode(type="instance", name="orphan_instance", component_ref="does_not_exist", width=10, height=10)
    plan = DesignPlan(screen_name="dangling ref test", elements=[orphan])

    fixed, notes = pv.validate_and_normalize(plan)
    assert fixed.elements[0].type == "frame", "Expected a dangling instance reference to be converted to a plain frame."
    assert fixed.elements[0].component_ref is None
    assert any("unknown component" in n.lower() for n in notes)

    # A VALID reference (component registered earlier in the same plan)
    # must be left completely alone.
    comp = DesignNode(type="component", name="btn", register_as="btn_component", width=10, height=10)
    inst = DesignNode(type="instance", name="btn_instance", component_ref="btn_component", width=10, height=10)
    plan2 = DesignPlan(screen_name="valid ref test", elements=[comp, inst])
    fixed2, notes2 = pv.validate_and_normalize(plan2)
    assert fixed2.elements[1].type == "instance"
    assert fixed2.elements[1].component_ref == "btn_component"
    assert not any("unknown component" in n.lower() for n in notes2)

    print("  confirmed: an instance referencing a nonexistent component is converted to a "
          "harmless placeholder frame and reported; a valid, resolvable reference is untouched.")
    print("test_dangling_instance_ref_becomes_placeholder_frame: PASSED\n")


async def test_valid_realistic_plan_produces_zero_notes():
    """Never 'corrects' something that isn't actually broken -- a
    well-formed plan built by the real semantic pipeline must pass
    through with an empty notes list."""
    print("Running test_valid_realistic_plan_produces_zero_notes...")

    simple = planner.SimplePlan(
        screen_name="Team Dashboard", theme="minimal_saas",
        elements=[
            planner.SimpleElement(type="header", content="Acme", items=["Dashboard", "Reports", "Settings"]),
            planner.SimpleElement(type="sidebar", items=["Dashboard", "Customers", "Orders", "Settings"]),
            planner.SimpleElement(type="heading", content="Overview", level="h1"),
            planner.SimpleElement(type="stat_card", content="Revenue", subtitle="$12,400", items=["+4.2%"]),
            planner.SimpleElement(type="table", items=["Name", "Status"], rows=[["Acme", "Paid"], ["Beta", "Pending"]]),
        ],
    )
    plan = planner.build_semantic_plan(simple, prompt="a minimal SaaS dashboard")
    _fixed, notes = pv.validate_and_normalize(plan)

    assert notes == [], f"Expected a valid, realistic semantic plan to produce zero validation notes, got: {notes}"

    print("  confirmed: a well-formed, realistic dashboard plan built by the real semantic "
          "pipeline passes through validation with ZERO corrections needed.")
    print("test_valid_realistic_plan_produces_zero_notes: PASSED\n")


async def test_does_not_mutate_caller_plan():
    print("Running test_does_not_mutate_caller_plan...")

    original = DesignNode(type="rectangle", name="r", width=99999, height=10)
    plan = DesignPlan(screen_name="mutation test", elements=[original])

    fixed, _notes = pv.validate_and_normalize(plan)

    assert plan.elements[0].width == 99999, "The original DesignPlan/DesignNode passed in must never be mutated."
    assert fixed.elements[0].width != 99999, "The returned plan must contain the corrected value."
    assert fixed is not plan

    print("  confirmed: validate_and_normalize returns a corrected COPY; the caller's "
          "original plan object is left completely unmodified.")
    print("test_does_not_mutate_caller_plan: PASSED\n")


async def main():
    await test_clamps_invalid_dimensions_and_reports_it()
    await test_deduplicates_top_level_names_only()
    await test_strips_auto_layout_from_non_capable_types()
    await test_rejects_excessively_large_plans()
    await test_resolves_overlapping_root_elements()
    await test_fills_missing_semantic_text_content()
    await test_dangling_instance_ref_becomes_placeholder_frame()
    await test_valid_realistic_plan_produces_zero_notes()
    await test_does_not_mutate_caller_plan()
    print("All test_plan_validation checks completed.")


if __name__ == "__main__":
    asyncio.run(main())
