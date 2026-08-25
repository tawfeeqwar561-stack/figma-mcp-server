"""
Phase 11 regression coverage: named content scenarios + sequential
generation + nested containers, through the REAL pipeline
(build_semantic_plan -> plan_validation -> plan_executor.execute_plan),
with bridge_client.send_figma_command mocked (fast, CI-safe, no live
bridge/Figma needed -- same convention as test_plan_size_timeout.py etc).

Run directly: python tests/test_regression_scenarios.py

Covers 9 of the task's 20 listed regression items directly:
  1. login          5. settings
  2. signup         6. ecommerce
  3. dashboard      7. dark theme
  4. profile        8. mobile screen
                     9. sequential screen generation (state isolation)
                    12. nested containers

The remaining 11 items are already covered by existing/other new test
files (preserved, not duplicated here):
  10. automatic canvas positioning -> tests/test_plugin_placement.js
  11. Auto Layout                  -> tests/test_components.py
  13. semantic components          -> tests/test_components.py
  14. design tokens                -> tests/test_design_tokens.py
  15. invalid plan normalization   -> tests/test_plan_validation.py
  16. bridge reconnect             -> tests/test_bridge_connection_reliability.py
  17. bridge retry                 -> tests/test_bridge_connection_reliability.py
  18. concurrent commands          -> tests/test_bridge_connection_reliability.py
  19. plugin disconnect            -> tests/test_plugin_disconnect_cleanup.py
  20. timeout                      -> tests/test_plan_size_timeout.py,
                                       tests/test_bridge_connection_reliability.py
"""

import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import asyncio
import uuid
from unittest.mock import AsyncMock, patch

import plan_executor
import planner
from design_plan import DesignNode

SimpleElement = planner.SimpleElement
SimplePlan = planner.SimplePlan


async def _mock_send_ok(action, payload):
    return {"status": "ok", "node_id": f"node-{uuid.uuid4().hex[:8]}"}


def _find_semantic(node: DesignNode, semantic: str) -> DesignNode | None:
    if node.semantic == semantic:
        return node
    for child in node.children:
        found = _find_semantic(child, semantic)
        if found:
            return found
    return None


def _max_depth(node: DesignNode) -> int:
    return 1 + max((_max_depth(c) for c in node.children), default=0)


async def _run_scenario(simple: SimplePlan, prompt: str):
    plan = planner.build_semantic_plan(simple, prompt=prompt)
    with patch.object(plan_executor.bridge_client, "send_figma_command", new=AsyncMock(side_effect=_mock_send_ok)):
        result = await plan_executor.execute_plan(plan)
    return plan, result


async def test_scenario_login():
    print("Running test_scenario_login...")
    simple = SimplePlan(screen_name="Login", theme="professional_blue", elements=[
        SimpleElement(type="heading", content="Welcome Back"),
        SimpleElement(type="input", content="Email"),
        SimpleElement(type="input", content="Password"),
        SimpleElement(type="button", content="Log In"),
    ])
    plan, result = await _run_scenario(simple, "a login screen with email and password")
    assert result.get("status") != "error" and result["failed"] == 0, f"login: unexpected failure: {result}"
    assert _find_semantic(plan.elements[0], "input") is not None
    assert _find_semantic(plan.elements[0], "button") is not None
    print(f"  confirmed: login screen executed with 0 failures ({result['succeeded']}/{result['total_nodes']} nodes).")
    print("test_scenario_login: PASSED\n")


async def test_scenario_signup():
    print("Running test_scenario_signup...")
    simple = SimplePlan(screen_name="Sign Up", theme="warm_friendly", elements=[
        SimpleElement(type="heading", content="Create Your Account"),
        SimpleElement(type="form", items=["Full Name", "Email", "Password"], subtitle="Create Account"),
    ])
    plan, result = await _run_scenario(simple, "a signup screen with name, email, and password fields")
    assert result.get("status") != "error" and result["failed"] == 0, f"signup: unexpected failure: {result}"
    form_node = _find_semantic(plan.elements[0], "form")
    assert form_node is not None, "Expected a 'form' composite in the signup screen."
    assert _find_semantic(form_node, "button") is not None, "Expected the form's submit button."
    print(f"  confirmed: signup screen (form with 3 fields + submit) executed with 0 failures "
          f"({result['succeeded']}/{result['total_nodes']} nodes).")
    print("test_scenario_signup: PASSED\n")


async def test_scenario_dashboard():
    print("Running test_scenario_dashboard...")
    simple = SimplePlan(screen_name="Dashboard", theme="minimal_saas", elements=[
        SimpleElement(type="header", content="Acme", items=["Dashboard", "Reports", "Settings"]),
        SimpleElement(type="sidebar", items=["Dashboard", "Customers", "Orders", "Settings"]),
        SimpleElement(type="heading", content="Overview"),
        SimpleElement(type="stat_card", content="Revenue", subtitle="$12,400", items=["+4.2%"]),
        SimpleElement(type="stat_card", content="Active Users", subtitle="342", items=["+1.1%"]),
        SimpleElement(type="table", items=["Customer", "Status"], rows=[["Acme Co", "Active"], ["Beta LLC", "Trial"]]),
    ])
    plan, result = await _run_scenario(simple, "a modern SaaS dashboard")
    assert result.get("status") != "error" and result["failed"] == 0, f"dashboard: unexpected failure: {result}"
    root = plan.elements[0]
    assert root.width == 1440, "Expected a desktop canvas for a dashboard."
    assert _find_semantic(root, "sidebar") is not None
    assert _find_semantic(root, "header") is not None
    assert _find_semantic(root, "table") is not None
    print(f"  confirmed: SaaS dashboard (header+sidebar+stat cards+table) executed with 0 "
          f"failures ({result['succeeded']}/{result['total_nodes']} nodes).")
    print("test_scenario_dashboard: PASSED\n")


async def test_scenario_profile():
    print("Running test_scenario_profile...")
    simple = SimplePlan(screen_name="Profile", theme="calm_wellness", elements=[
        SimpleElement(type="avatar", content="JD"),
        SimpleElement(type="heading", content="Jordan Diaz"),
        SimpleElement(type="text", content="jordan@example.com"),
        SimpleElement(type="card", content="About", subtitle="Wellness coach based in Austin, TX."),
        SimpleElement(type="button", content="Edit Profile"),
    ])
    plan, result = await _run_scenario(simple, "a user profile screen")
    assert result.get("status") != "error" and result["failed"] == 0, f"profile: unexpected failure: {result}"
    assert _find_semantic(plan.elements[0], "avatar") is not None
    assert _find_semantic(plan.elements[0], "card") is not None
    print(f"  confirmed: profile screen (avatar+card+button) executed with 0 failures "
          f"({result['succeeded']}/{result['total_nodes']} nodes).")
    print("test_scenario_profile: PASSED\n")


async def test_scenario_settings():
    print("Running test_scenario_settings...")
    simple = SimplePlan(screen_name="Settings", theme="professional_blue", elements=[
        SimpleElement(type="heading", content="Settings"),
        SimpleElement(type="list", items=["Account", "Notifications", "Privacy", "Billing"]),
        SimpleElement(type="divider"),
        SimpleElement(type="button", content="Log Out", variant="outline"),
    ])
    plan, result = await _run_scenario(simple, "a settings screen with account options")
    assert result.get("status") != "error" and result["failed"] == 0, f"settings: unexpected failure: {result}"
    assert _find_semantic(plan.elements[0], "list") is not None
    print(f"  confirmed: settings screen (list+divider+button) executed with 0 failures "
          f"({result['succeeded']}/{result['total_nodes']} nodes).")
    print("test_scenario_settings: PASSED\n")


async def test_scenario_ecommerce():
    print("Running test_scenario_ecommerce...")
    simple = SimplePlan(screen_name="Product", theme="modern_ecommerce", elements=[
        SimpleElement(type="header", content="Acme Shop", items=["Home", "Shop", "Cart"]),
        SimpleElement(type="image", content="Product photo"),
        SimpleElement(type="heading", content="Wireless Headphones"),
        SimpleElement(type="text", content="$129.00"),
        SimpleElement(type="badge", content="In Stock", variant="success"),
        SimpleElement(type="button", content="Add to Cart"),
    ])
    plan, result = await _run_scenario(simple, "a modern ecommerce product page")
    assert result.get("status") != "error" and result["failed"] == 0, f"ecommerce: unexpected failure: {result}"
    assert plan.design_system == "modern_ecommerce"
    assert _find_semantic(plan.elements[0], "image") is not None
    assert _find_semantic(plan.elements[0], "badge") is not None
    print(f"  confirmed: ecommerce product page (image+badge+cart button) executed with 0 "
          f"failures ({result['succeeded']}/{result['total_nodes']} nodes), design_system="
          f"'{plan.design_system}'.")
    print("test_scenario_ecommerce: PASSED\n")


async def test_scenario_dark_theme():
    print("Running test_scenario_dark_theme...")
    simple = SimplePlan(screen_name="Trading Dashboard", theme="dark_fintech", elements=[
        SimpleElement(type="header", content="Nova Trade", items=["Markets", "Portfolio"]),
        SimpleElement(type="stat_card", content="Portfolio Value", subtitle="$48,204.12", items=["+2.8%"]),
        SimpleElement(type="table", items=["Asset", "Price"], rows=[["BTC", "$61,204"], ["ETH", "$3,412"]]),
    ])
    plan, result = await _run_scenario(simple, "a dark fintech trading dashboard")
    assert result.get("status") != "error" and result["failed"] == 0, f"dark theme: unexpected failure: {result}"
    root = plan.elements[0]
    assert plan.design_system == "dark_fintech"
    bg = root.color
    luminance = 0.299 * bg.r + 0.587 * bg.g + 0.114 * bg.b
    assert luminance < 0.3, f"Expected a genuinely dark background, got luminance {luminance:.2f}"
    print(f"  confirmed: dark fintech dashboard executed with 0 failures "
          f"({result['succeeded']}/{result['total_nodes']} nodes) with a genuinely dark "
          f"background (luminance={luminance:.2f}).")
    print("test_scenario_dark_theme: PASSED\n")


async def test_scenario_mobile_screen():
    print("Running test_scenario_mobile_screen...")
    simple = SimplePlan(screen_name="Onboarding", theme="healthcare_mobile", elements=[
        SimpleElement(type="heading", content="Welcome to CareTrack"),
        SimpleElement(type="text", content="Track your appointments and prescriptions in one place."),
        SimpleElement(type="button", content="Get Started"),
    ])
    plan, result = await _run_scenario(simple, "a healthcare mobile app onboarding screen")
    assert result.get("status") != "error" and result["failed"] == 0, f"mobile screen: unexpected failure: {result}"
    root = plan.elements[0]
    assert root.width == 375 and root.height == 812, f"Expected a mobile canvas (375x812), got {root.width}x{root.height}"
    print(f"  confirmed: healthcare mobile onboarding screen executed with 0 failures "
          f"({result['succeeded']}/{result['total_nodes']} nodes) at mobile size "
          f"{root.width}x{root.height}.")
    print("test_scenario_mobile_screen: PASSED\n")


async def test_nested_containers_depth():
    """Phase 4 requirement: nested frames/containers for page, header,
    sidebar, content, sections, cards -- verify real, multi-level nesting
    exists in a realistic dashboard, not a flat list of siblings."""
    print("Running test_nested_containers_depth...")

    simple = SimplePlan(screen_name="Dashboard", theme="minimal_saas", elements=[
        SimpleElement(type="header", content="Acme", items=["Dashboard"]),
        SimpleElement(type="sidebar", items=["Dashboard", "Settings"]),
        SimpleElement(type="stat_card", content="Revenue", subtitle="$12,400"),
    ])
    plan = planner.build_semantic_plan(simple, prompt="a minimal SaaS dashboard")
    depth = _max_depth(plan.elements[0])
    # page > body > content > stat_card(card) > text  == at least 5 levels.
    assert depth >= 5, f"Expected at least 5 levels of nesting (page/body/content/card/text), got {depth}"

    print(f"  confirmed: a realistic dashboard nests {depth} levels deep "
          f"(page > body > sidebar-or-content > card > text), not a flat sibling list.")
    print("test_nested_containers_depth: PASSED\n")


async def test_sequential_generation_state_isolation():
    """Phase 5/9 companion (Python-level): running execute_plan multiple
    times in a row for DIFFERENT screens must not leak component_registry
    or created_node_ids state between calls -- each call's per-execution
    dicts must be genuinely fresh (see plan_executor.execute_plan's own
    created_node_ids/component_registry, both explicitly local per call)."""
    print("Running test_sequential_generation_state_isolation...")

    def _plan_with_reused_component_name():
        comp = DesignNode(type="component", name="btn", register_as="shared_name", width=80, height=32)
        inst = DesignNode(type="instance", name="btn2", component_ref="shared_name", width=80, height=32)
        from design_plan import DesignPlan
        return DesignPlan(screen_name="scenario", elements=[comp, inst])

    with patch.object(plan_executor.bridge_client, "send_figma_command", new=AsyncMock(side_effect=_mock_send_ok)):
        results = []
        for _ in range(4):  # simulate "dashboard, login, profile, settings" sequentially
            plan = _plan_with_reused_component_name()
            result = await plan_executor.execute_plan(plan)
            results.append(result)

    for i, result in enumerate(results):
        assert result.get("status") != "error", f"Screen {i}: unexpected error shape: {result}"
        assert result["failed"] == 0, (
            f"Screen {i}: instance failed to resolve its OWN plan's component "
            f"(got failed={result['failed']}) -- suggests component_registry state leaked "
            f"or was incorrectly shared across sequential execute_plan calls: {result}"
        )
        assert result["succeeded"] == 2, f"Screen {i}: expected both component+instance to succeed, got {result}"

    print(f"  confirmed: 4 sequential execute_plan calls (simulating dashboard->login->"
          f"profile->settings), each reusing the SAME logical component name 'shared_name', "
          f"all independently resolved correctly with 0 failures -- no state leakage across calls.")
    print("test_sequential_generation_state_isolation: PASSED\n")


async def main():
    await test_scenario_login()
    await test_scenario_signup()
    await test_scenario_dashboard()
    await test_scenario_profile()
    await test_scenario_settings()
    await test_scenario_ecommerce()
    await test_scenario_dark_theme()
    await test_scenario_mobile_screen()
    await test_nested_containers_depth()
    await test_sequential_generation_state_isolation()
    print("All test_regression_scenarios checks completed.")


if __name__ == "__main__":
    asyncio.run(main())
