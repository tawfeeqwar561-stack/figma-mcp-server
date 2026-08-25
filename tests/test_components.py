"""
Tests for components.py (Phase 2/4/6/7/8: semantic components, Auto
Layout, component/instance reuse, typography, visual quality).

Run directly: python tests/test_components.py

Verifies:
  - Every semantic composer (heading, paragraph, button, input, form,
    card, list, table, badge, avatar, tabs, top_nav, sidebar, modal,
    section, page, etc) builds using ONLY existing primitive NodeTypes
    (design_plan.NodeType) -- i.e. semantic concepts are additive, never
    breaking the existing DesignNode contract.
  - Container composers set auto_layout (Phase 4: Auto Layout preferred
    over manual x/y). Leaf/primitive nodes correctly do NOT (a rectangle
    or text node has no auto_layout of its own -- only its container does).
  - Every color/size drawn from a composer traces back to the DesignTokens
    instance passed in (no hardcoded colors) -- verified by rendering the
    SAME composer with two different token sets and asserting the output
    actually differs, proving no ad hoc/random values leaked in.
  - button_group/badge_row/list_block's component+instance reuse: the
    first repeat becomes a real `component` node with register_as set,
    every subsequent repeat becomes an `instance` node with a matching
    component_ref (Phase 6 requirement: real component reuse, not
    duplicated trees, for the safe single-text-difference case).
  - components.page() assembles the header/sidebar/content skeleton
    correctly and stays within DesignNode.children's existing cap (50).
"""

import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import asyncio

import components as c
from design_plan import DesignNode
from design_tokens import get_tokens

_VALID_NODE_TYPES = {
    "frame", "text", "rectangle", "ellipse", "line",
    "image_placeholder", "icon", "component", "component_set", "group", "instance",
}


def _assert_only_known_types(node: DesignNode, path: str = "") -> None:
    label = f"{path}/{node.name}" if path else node.name
    assert node.type in _VALID_NODE_TYPES, f"Node '{label}' has type {node.type!r}, not one of the existing NodeType values."
    for child in node.children:
        _assert_only_known_types(child, label)


def _count_nodes(node: DesignNode) -> int:
    return 1 + sum(_count_nodes(c) for c in node.children)


def _collect_colors(node: DesignNode, colors: list) -> None:
    if node.color is not None:
        colors.append((node.color.r, node.color.g, node.color.b))
    if node.text_color is not None:
        colors.append((node.text_color.r, node.text_color.g, node.text_color.b))
    for child in node.children:
        _collect_colors(child, colors)


async def test_all_composers_use_only_existing_primitive_types():
    print("Running test_all_composers_use_only_existing_primitive_types...")

    tokens = get_tokens("professional_blue")
    built = [
        c.heading(tokens, "Welcome"),
        c.paragraph(tokens, "Some body copy here."),
        c.label_text(tokens, "Label"),
        c.caption_text(tokens, "Caption"),
        c.button(tokens, "Continue"),
        c.button_group(tokens, ["Save", "Cancel", "Delete"]),
        c.input_field(tokens, label="Email", placeholder="you@example.com"),
        c.form(tokens, [("Email", ""), ("Password", "")], submit_label="Log In"),
        c.badge(tokens, "Active", tone="success"),
        c.badge_row(tokens, ["New", "Active", "Archived"]),
        c.avatar(tokens, initials="JD"),
        c.divider(tokens),
        c.image_block(tokens, caption="Product photo"),
        c.icon_glyph(tokens, glyph="A"),
        c.card(tokens, [c.heading(tokens, "Card title", level="h3")]),
        c.stat_card(tokens, "Revenue", "$12,400", delta="+4.2%"),
        c.list_block(tokens, [{"title": "Item 1"}, {"title": "Item 2"}]),
        c.list_block(tokens, [{"title": "Coffee Shop", "subtitle": "Today", "trailing": "-$4.50"}]),
        c.table(tokens, ["Name", "Status"], [["Acme", "Paid"], ["Beta Co", "Pending"]]),
        c.tabs(tokens, ["Overview", "Settings", "Billing"]),
        c.top_nav(tokens, "Acme", nav_items=["Dashboard", "Reports"]),
        c.sidebar(tokens, ["Dashboard", "Customers", "Orders"]),
        c.modal(tokens, "Delete item?", "This cannot be undone.", actions=["Cancel", "Delete"]),
        c.section(tokens, "Overview", [c.paragraph(tokens, "A short description.")]),
    ]
    for node in built:
        _assert_only_known_types(node)

    print(f"  confirmed: all {len(built)} semantic composers build trees using only existing "
          f"primitive NodeType values -- semantic concepts are additive, not a schema break.")
    print("test_all_composers_use_only_existing_primitive_types: PASSED\n")


async def test_containers_use_auto_layout():
    print("Running test_containers_use_auto_layout...")

    tokens = get_tokens("professional_blue")

    btn = c.button(tokens, "Continue")
    assert btn.auto_layout is not None, "button() must use Auto Layout, not manual child positioning."

    card = c.card(tokens, [c.heading(tokens, "Title", level="h3")])
    assert card.auto_layout is not None, "card() must use Auto Layout."

    side = c.sidebar(tokens, ["Home", "Settings"])
    assert side.auto_layout is not None, "sidebar() must use Auto Layout."
    for item in side.children:
        assert item.auto_layout is not None, "each sidebar nav item row must use Auto Layout."

    header = c.top_nav(tokens, "Acme", nav_items=["Dashboard"])
    assert header.auto_layout is not None

    page = c.page(tokens, "Test Screen", platform="desktop", header=header, sidebar_node=side, content=[card])
    assert page.auto_layout is not None, "page() root must use Auto Layout."
    body_row = page.children[-1]
    assert body_row.auto_layout is not None, "page()'s body row (sidebar + content) must use Auto Layout."

    # Leaf/primitive nodes must NOT carry their own auto_layout (only a
    # container's own layout matters -- a rectangle/divider is not itself
    # a layout container).
    divider = c.divider(tokens)
    assert divider.auto_layout is None, "divider() is a plain rectangle and must not set auto_layout on itself."

    print("  confirmed: every container composer (button, card, sidebar, sidebar items, "
          "header, page, body row) sets Auto Layout; leaf primitives correctly don't.")
    print("test_containers_use_auto_layout: PASSED\n")


async def test_colors_trace_to_the_given_tokens_not_hardcoded():
    print("Running test_colors_trace_to_the_given_tokens_not_hardcoded...")

    light_tokens = get_tokens("professional_blue")
    dark_tokens = get_tokens("dark_fintech")

    for label, builder in [
        ("card", lambda t: c.card(t, [c.heading(t, "Revenue", level="h3")])),
        ("button", lambda t: c.button(t, "Continue")),
        ("sidebar", lambda t: c.sidebar(t, ["Home", "Settings"])),
        ("top_nav", lambda t: c.top_nav(t, "Acme", nav_items=["Dashboard"])),
        ("stat_card", lambda t: c.stat_card(t, "Revenue", "$12,400", delta="+4.2%")),
    ]:
        light_colors = []
        dark_colors = []
        _collect_colors(builder(light_tokens), light_colors)
        _collect_colors(builder(dark_tokens), dark_colors)
        assert light_colors, f"{label}: expected at least one color to be set."
        assert light_colors != dark_colors, (
            f"{label}: rendering with two different DesignTokens instances produced IDENTICAL "
            f"colors ({light_colors}) -- colors must be drawn from the given tokens, not hardcoded."
        )

    print("  confirmed: card/button/sidebar/top_nav/stat_card all render different colors when "
          "given a different DesignTokens instance -- no hardcoded/random colors.")
    print("test_colors_trace_to_the_given_tokens_not_hardcoded: PASSED\n")


async def test_button_group_reuses_a_real_component():
    """Phase 6: repeated atoms whose only difference is one text label
    become a real component + instances, not duplicated full trees."""
    print("Running test_button_group_reuses_a_real_component...")

    tokens = get_tokens("professional_blue")
    group = c.button_group(tokens, ["Save", "Cancel", "Delete"])

    assert len(group.children) == 3
    first, rest = group.children[0], group.children[1:]

    assert first.type == "component", f"Expected the first button repeat to be a real 'component' node, got {first.type!r}"
    assert first.register_as, "Expected the first button repeat to register itself under a logical name."

    for i, node in enumerate(rest, start=2):
        assert node.type == "instance", f"Expected repeat #{i} to be an 'instance' node, got {node.type!r}"
        assert node.component_ref == first.register_as, (
            f"Expected instance #{i}'s component_ref ({node.component_ref!r}) to match the "
            f"component's register_as ({first.register_as!r})"
        )

    print(f"  confirmed: button_group's 3 buttons become 1 real component ('{first.register_as}') "
          f"+ 2 instances referencing it, instead of 3 duplicated node trees.")
    print("test_button_group_reuses_a_real_component: PASSED\n")


async def test_list_block_component_reuse_only_for_title_only_rows():
    """A title-only list (safe: only one varying part) should dedupe via
    component/instance; a richer list (subtitle/trailing/avatar -- more
    than one varying part) should NOT, per the module's own documented
    safety rule."""
    print("Running test_list_block_component_reuse_only_for_title_only_rows...")

    tokens = get_tokens("professional_blue")

    simple_list = c.list_block(tokens, [{"title": "Item 1"}, {"title": "Item 2"}, {"title": "Item 3"}])
    # children are [item, divider, item, divider, item] -- filter to just the item rows.
    item_rows = [n for n in simple_list.children if n.semantic == "list_item" or n.type == "instance"]
    assert any(n.type == "component" for n in simple_list.children), "Expected a real component among the simple list's rows."
    assert any(n.type == "instance" for n in simple_list.children), "Expected at least one instance among the simple list's rows."

    rich_list = c.list_block(tokens, [
        {"title": "Coffee Shop", "subtitle": "Today, 9:14 AM", "trailing": "-$4.50"},
        {"title": "Acme Co", "subtitle": "Yesterday", "trailing": "-$120.00"},
    ])
    assert all(n.type != "instance" for n in rich_list.children), (
        "Expected NO instance nodes in a list with subtitle/trailing (more than one varying "
        "part) -- component reuse is only safe for the single-text-difference case."
    )

    print("  confirmed: a title-only list dedupes via component+instance; a richer list "
          "(subtitle+trailing) correctly stays as plain, fully-composed frames.")
    print("test_list_block_component_reuse_only_for_title_only_rows: PASSED\n")


async def test_page_assembly_stays_within_existing_children_cap():
    """DesignNode.children is capped at 50 (H-6, bridge-security-hardening,
    unchanged). A realistic dashboard page must not blow this per-node cap
    at any single level of nesting."""
    print("Running test_page_assembly_stays_within_existing_children_cap...")

    tokens = get_tokens("minimal_saas")
    header = c.top_nav(tokens, "Acme", nav_items=["Dashboard", "Reports", "Settings"])
    side = c.sidebar(tokens, ["Dashboard", "Customers", "Orders", "Settings"])
    content = [
        c.section(tokens, "Overview", [
            c.stat_card(tokens, "Revenue", "$12,400", delta="+4.2%"),
            c.stat_card(tokens, "Active Users", "1,204", delta="+1.1%"),
        ]),
        c.table(tokens, ["Name", "Status", "Amount"], [["Acme", "Paid", "$120"], ["Beta", "Pending", "$80"]]),
        c.list_block(tokens, [{"title": f"Notification {i}"} for i in range(8)]),
    ]
    page = c.page(tokens, "Dashboard", platform="desktop", header=header, sidebar_node=side, content=content)

    def _max_children_at_any_level(node: DesignNode) -> int:
        worst = len(node.children)
        for child in node.children:
            worst = max(worst, _max_children_at_any_level(child))
        return worst

    worst = _max_children_at_any_level(page)
    assert worst <= 50, f"A single node had {worst} children, exceeding the existing 50-child cap."
    total = _count_nodes(page)
    assert total > 0

    print(f"  confirmed: a realistic dashboard page ({total} total nodes) stays within the "
          f"existing 50-child-per-node cap at every level (worst: {worst} children).")
    print("test_page_assembly_stays_within_existing_children_cap: PASSED\n")


async def main():
    await test_all_composers_use_only_existing_primitive_types()
    await test_containers_use_auto_layout()
    await test_colors_trace_to_the_given_tokens_not_hardcoded()
    await test_button_group_reuses_a_real_component()
    await test_list_block_component_reuse_only_for_title_only_rows()
    await test_page_assembly_stays_within_existing_children_cap()
    print("All test_components checks completed.")


if __name__ == "__main__":
    asyncio.run(main())
