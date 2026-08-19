"""
Tests for Subsystem 6 -- Plan size/timeout caps (H-6: unbounded plan size/time).

Run directly: python tests/test_plan_size_timeout.py

Contains BOTH:
  - test_bug_condition_unbounded_size_and_time(): Property 1 (Bug
    Condition). On UNFIXED code, proves a DesignNode with 51 children and
    a SimplePlan with 11 elements both construct without a
    pydantic.ValidationError, and that execute_plan has no internal
    timeout (a never-returning mocked bridge call hangs past a harness
    deadline). After the fix, the same constructions raise
    ValidationError, and execute_plan returns a timeout error dict
    promptly.
  - test_preservation_full_tree_within_limits(): Property 2
    (Preservation). Runs the existing _login_template()/_dashboard_template()
    plans (well under any cap) through execute_plan with a mocked bridge,
    and seeded-random within-cap shapes, asserting identical
    succeeded/failed/total_nodes shape both before and after the fix.
"""

import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import asyncio
import random
import uuid
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError

import config
import plan_executor
import planner
from design_plan import DesignNode, DesignPlan


def _fix_present() -> bool:
    """Detect the fix by checking for the new config knob (added alongside the caps)."""
    return hasattr(config, "PLAN_EXECUTION_TIMEOUT_SECONDS")


async def _mock_send_figma_command_ok(action, payload):
    return {"status": "ok", "node_id": f"node-{uuid.uuid4().hex[:8]}"}


async def test_bug_condition_unbounded_size_and_time():
    """Property 1: Bug Condition -- Unbounded Plan Size/Time (H-6)."""
    print("Running test_bug_condition_unbounded_size_and_time...")

    fix_present = _fix_present()

    # --- Case 1: DesignNode with 51 children ---
    many_children = [DesignNode(type="rectangle", width=1, height=1) for _ in range(51)]
    if fix_present:
        raised = False
        try:
            DesignNode(type="frame", children=many_children)
        except ValidationError:
            raised = True
        assert raised, "Expected FIXED DesignNode to raise ValidationError for 51 children (cap is 50)."
        print("  [FIXED] confirmed: DesignNode with 51 children raises ValidationError.")
    else:
        node = DesignNode(type="frame", children=many_children)
        assert len(node.children) == 51, (
            "Expected UNFIXED DesignNode to accept 51 children with no cap (confirms H-6)."
        )
        print("  [UNFIXED] confirmed: DesignNode with 51 children constructed successfully, "
              "no ValidationError -- counterexample for H-6.")

    # --- Case 2: SimplePlan with 11 elements ---
    many_elements = [planner.SimpleElement(type="text", content=f"c{i}") for i in range(11)]
    if fix_present:
        raised = False
        try:
            planner.SimplePlan(screen_name="Big Plan", elements=many_elements)
        except ValidationError:
            raised = True
        assert raised, "Expected FIXED SimplePlan to raise ValidationError for 11 elements (cap is 10)."
        print("  [FIXED] confirmed: SimplePlan with 11 elements raises ValidationError.")
    else:
        simple = planner.SimplePlan(screen_name="Big Plan", elements=many_elements)
        assert len(simple.elements) == 11, (
            "Expected UNFIXED SimplePlan to accept 11 elements with no cap (confirms H-6)."
        )
        print("  [UNFIXED] confirmed: SimplePlan with 11 elements constructed successfully, "
              "no ValidationError -- counterexample for H-6.")

    # --- Case 3: execute_plan has no overall timeout ---
    hang_seconds = 3.0  # long enough to prove "never completes promptly", short enough for a test

    async def _hanging_send_figma_command(action, payload):
        await asyncio.sleep(hang_seconds)
        return {"status": "ok", "node_id": "never-reached"}

    plan = DesignPlan(screen_name="Hang Plan", elements=[DesignNode(type="rectangle", width=1, height=1)])

    with patch.object(plan_executor.bridge_client, "send_figma_command", new=AsyncMock(side_effect=_hanging_send_figma_command)):
        if fix_present:
            original_timeout = config.PLAN_EXECUTION_TIMEOUT_SECONDS
            config.PLAN_EXECUTION_TIMEOUT_SECONDS = 0.2  # short override for the test
            try:
                start = asyncio.get_event_loop().time()
                result = await plan_executor.execute_plan(plan)
                elapsed = asyncio.get_event_loop().time() - start
                assert result.get("status") == "error", (
                    f"Expected FIXED execute_plan to return a timeout error dict, got {result}"
                )
                assert "timed out" in result.get("message", "").lower()
                assert elapsed < hang_seconds, (
                    f"Expected FIXED execute_plan to return promptly on timeout, took {elapsed:.2f}s"
                )
                print(f"  [FIXED] confirmed: execute_plan returns a timeout error dict promptly "
                      f"({elapsed:.2f}s) instead of hanging for {hang_seconds}s.")
            finally:
                config.PLAN_EXECUTION_TIMEOUT_SECONDS = original_timeout
        else:
            # Harness-level deadline only, to keep the test itself terminable --
            # NOT part of the code under test.
            try:
                await asyncio.wait_for(plan_executor.execute_plan(plan), timeout=0.5)
                never_completed = False
            except asyncio.TimeoutError:
                never_completed = True
            assert never_completed, (
                "Expected UNFIXED execute_plan to have no internal timeout, so it should "
                "still be running past our short harness deadline (confirms H-6)."
            )
            print("  [UNFIXED] confirmed: execute_plan has no internal timeout and is still "
                  "running past a short harness deadline -- counterexample for H-6.")

    print("test_bug_condition_unbounded_size_and_time: PASSED\n")


def _random_within_cap_node(rng, depth, max_depth=2):
    node_type = rng.choice(["rectangle", "text", "frame"])
    children = []
    if node_type == "frame" and depth < max_depth:
        for _ in range(rng.randint(0, 3)):
            children.append(_random_within_cap_node(rng, depth + 1, max_depth))
    kwargs = dict(type=node_type, name=f"n_{rng.randint(0, 99999)}", width=rng.randint(10, 200), height=rng.randint(10, 200))
    if node_type == "text":
        kwargs["content"] = "x" * rng.randint(1, 200)  # comfortably under the 500/2000 caps
    if children:
        kwargs["children"] = children
    return DesignNode(**kwargs)


async def test_preservation_full_tree_within_limits():
    """Property 2: Preservation -- Full Node Tree Execution Within Limits."""
    print("Running test_preservation_full_tree_within_limits...")

    with patch.object(plan_executor.bridge_client, "send_figma_command", new=AsyncMock(side_effect=_mock_send_figma_command_ok)):
        for name, plan in [
            ("login_template", planner._login_template()),
            ("dashboard_template", planner._dashboard_template()),
        ]:
            result = await plan_executor.execute_plan(plan)
            assert result.get("failed", None) == 0, f"{name}: expected 0 failures, got {result}"
            assert result["succeeded"] == result["total_nodes"], f"{name}: succeeded != total_nodes"
            print(f"  confirmed: {name} builds the full node tree with 0 failures "
                  f"({result['succeeded']}/{result['total_nodes']} succeeded).")

        rng = random.Random(777)  # seeded for reproducibility
        for trial in range(5):
            elements = [_random_within_cap_node(rng, depth=0) for _ in range(rng.randint(1, 3))]
            plan = DesignPlan(screen_name=f"trial-{trial}", elements=elements)
            result = await plan_executor.execute_plan(plan)
            assert result["failed"] == 0, f"Trial {trial}: expected 0 failures, got {result}"
            assert result["succeeded"] == result["total_nodes"], f"Trial {trial}: succeeded != total_nodes"

    print("  confirmed: randomly-shaped within-cap plans build successfully with matching "
          "succeeded/total_nodes counts, on both pre-fix and post-fix code.")
    print("test_preservation_full_tree_within_limits: PASSED\n")


async def main():
    await test_bug_condition_unbounded_size_and_time()
    await test_preservation_full_tree_within_limits()
    print("All test_plan_size_timeout checks completed.")


if __name__ == "__main__":
    asyncio.run(main())
