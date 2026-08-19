"""
Tests for the H-6 follow-up fix -- per-command failure isolation in
plan_executor.py.

Run directly: python tests/test_plan_command_failure_isolation.py

Context: execute_plan()'s _run() closure is wrapped in
`await asyncio.wait_for(_run(), timeout=config.PLAN_EXECUTION_TIMEOUT_SECONDS)`
with `except asyncio.TimeoutError:`. Since Python 3.11, `TimeoutError` and
`asyncio.TimeoutError` are the same exception class. bridge_client.send_figma_command
independently raises a plain TimeoutError (via its own
RESPONSE_TIMEOUT_SECONDS = 10.0 wait) whenever no plugin is connected to
respond -- an expected, everyday occurrence, not a plan-size problem. Before
this fix, that per-command TimeoutError propagated up through _run() and was
caught by the SAME except clause guarding the outer 120s deadline, producing
a misleading "Plan execution timed out after 120.0s" message after only
~10 seconds and discarding all partial results.

This file complements (does not replace) tests/test_plan_size_timeout.py,
which already covers the genuine-overall-timeout case (a mocked bridge call
that hangs past config.PLAN_EXECUTION_TIMEOUT_SECONDS). This file covers the
new per-command-failure-isolation behavior only:
  - test_single_node_timeout_is_isolated(): one node's mocked
    send_figma_command raises TimeoutError; other nodes succeed. Asserts
    execute_plan returns a normal result dict (NOT the blanket "timed out"
    shape), with the failing node counted in `failed` and the rest in
    `succeeded`.
  - test_single_node_connection_error_is_isolated(): same, but with
    ConnectionError instead of TimeoutError.
  - test_preservation_all_succeed(): reuses the existing
    _login_template()/_dashboard_template() pattern from
    test_plan_size_timeout.py -- every mocked call succeeds, and the
    succeeded/failed/total_nodes shape is unchanged by this fix.
"""

import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import asyncio
import uuid
from unittest.mock import AsyncMock, patch

import plan_executor
import planner
from design_plan import DesignNode, DesignPlan


async def _mock_send_figma_command_ok(action, payload):
    return {"status": "ok", "node_id": f"node-{uuid.uuid4().hex[:8]}"}


def _make_three_node_plan() -> DesignPlan:
    """Three independent top-level rectangles -- no parent/child coupling,
    so one node's failure cannot cascade into another's via the allowlist."""
    return DesignPlan(
        screen_name="Three Node Plan",
        elements=[
            DesignNode(type="rectangle", name="rect_a", width=10, height=10),
            DesignNode(type="rectangle", name="rect_b", width=10, height=10),
            DesignNode(type="rectangle", name="rect_c", width=10, height=10),
        ],
    )


async def test_single_node_timeout_is_isolated():
    """A single node's TimeoutError must not be mistaken for the overall
    execute_plan deadline, and must not discard the other nodes' results."""
    print("Running test_single_node_timeout_is_isolated...")

    call_count = {"n": 0}

    async def _flaky_send(action, payload):
        call_count["n"] += 1
        if call_count["n"] == 2:  # the second node fails
            raise TimeoutError("simulated: no plugin connected")
        return {"status": "ok", "node_id": f"node-{uuid.uuid4().hex[:8]}"}

    plan = _make_three_node_plan()

    with patch.object(plan_executor.bridge_client, "send_figma_command", new=AsyncMock(side_effect=_flaky_send)):
        result = await plan_executor.execute_plan(plan)

    # Must NOT be the blanket outer-timeout error shape.
    assert result.get("status") != "error", (
        f"Expected a normal per-node result dict, got the blanket error shape: {result}"
    )
    assert "timed out" not in result.get("message", "").lower(), (
        f"Expected no misleading 'timed out' message, got: {result}"
    )

    # Must be the normal execute_plan result shape.
    assert "total_nodes" in result and "succeeded" in result and "failed" in result, (
        f"Expected normal execute_plan result shape with total_nodes/succeeded/failed, got: {result}"
    )
    assert result["total_nodes"] == 3, f"Expected 3 total nodes, got {result}"
    assert result["failed"] == 1, f"Expected exactly 1 failed node, got {result}"
    assert result["succeeded"] == 2, f"Expected exactly 2 succeeded nodes, got {result}"

    failed_results = [r for r in result["results"] if r.get("status") != "ok"]
    assert len(failed_results) == 1, f"Expected exactly 1 non-ok result entry, got {failed_results}"
    assert "simulated: no plugin connected" in failed_results[0].get("message", ""), (
        f"Expected the original TimeoutError message preserved on the failing node, got {failed_results[0]}"
    )

    print("  confirmed: a single node's TimeoutError is isolated to that node's own "
          f"failed/succeeded accounting (succeeded={result['succeeded']}, failed={result['failed']}, "
          f"total_nodes={result['total_nodes']}), not mistaken for the overall plan timeout.")
    print("test_single_node_timeout_is_isolated: PASSED\n")


async def test_single_node_connection_error_is_isolated():
    """Same as above, but for ConnectionError (bridge unreachable)."""
    print("Running test_single_node_connection_error_is_isolated...")

    call_count = {"n": 0}

    async def _flaky_send(action, payload):
        call_count["n"] += 1
        if call_count["n"] == 2:  # the second node fails
            raise ConnectionError("simulated: bridge unreachable")
        return {"status": "ok", "node_id": f"node-{uuid.uuid4().hex[:8]}"}

    plan = _make_three_node_plan()

    with patch.object(plan_executor.bridge_client, "send_figma_command", new=AsyncMock(side_effect=_flaky_send)):
        result = await plan_executor.execute_plan(plan)

    assert result.get("status") != "error", (
        f"Expected a normal per-node result dict, got the blanket error shape: {result}"
    )
    assert "total_nodes" in result and "succeeded" in result and "failed" in result, (
        f"Expected normal execute_plan result shape, got: {result}"
    )
    assert result["total_nodes"] == 3, f"Expected 3 total nodes, got {result}"
    assert result["failed"] == 1, f"Expected exactly 1 failed node, got {result}"
    assert result["succeeded"] == 2, f"Expected exactly 2 succeeded nodes, got {result}"

    failed_results = [r for r in result["results"] if r.get("status") != "ok"]
    assert len(failed_results) == 1, f"Expected exactly 1 non-ok result entry, got {failed_results}"
    assert "simulated: bridge unreachable" in failed_results[0].get("message", ""), (
        f"Expected the original ConnectionError message preserved on the failing node, got {failed_results[0]}"
    )

    print("  confirmed: a single node's ConnectionError is isolated the same way "
          f"(succeeded={result['succeeded']}, failed={result['failed']}, total_nodes={result['total_nodes']}).")
    print("test_single_node_connection_error_is_isolated: PASSED\n")


async def test_preservation_all_succeed():
    """Preservation: when every command succeeds, this fix changes nothing --
    identical succeeded/failed/total_nodes shape as before, using the same
    template plans test_plan_size_timeout.py already validates."""
    print("Running test_preservation_all_succeed...")

    with patch.object(plan_executor.bridge_client, "send_figma_command", new=AsyncMock(side_effect=_mock_send_figma_command_ok)):
        for name, plan in [
            ("login_template", planner._login_template()),
            ("dashboard_template", planner._dashboard_template()),
        ]:
            result = await plan_executor.execute_plan(plan)
            assert result.get("status") != "error", f"{name}: unexpected error shape: {result}"
            assert result.get("failed", None) == 0, f"{name}: expected 0 failures, got {result}"
            assert result["succeeded"] == result["total_nodes"], f"{name}: succeeded != total_nodes"
            print(f"  confirmed: {name} builds the full node tree with 0 failures "
                  f"({result['succeeded']}/{result['total_nodes']} succeeded), unchanged by this fix.")

    print("test_preservation_all_succeed: PASSED\n")


async def main():
    await test_single_node_timeout_is_isolated()
    await test_single_node_connection_error_is_isolated()
    await test_preservation_all_succeed()
    print("All test_plan_command_failure_isolation checks completed.")


if __name__ == "__main__":
    asyncio.run(main())
