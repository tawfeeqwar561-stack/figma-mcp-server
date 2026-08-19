"""
Tests for Subsystem 2 -- Parent_id allowlist (C-2: unrestricted parent_id).

Run directly: python tests/test_parent_allowlist.py

Contains BOTH:
  - test_bug_condition_unrestricted_parent_id(): Property 1 (Bug Condition).
    On UNFIXED code, proves execute_node forwards an arbitrary parent_id
    (never returned by any mocked send_figma_command call) straight to
    bridge_client.send_figma_command with no check. After the fix, the
    same assertions flip to confirm the node is rejected instead.
  - test_preservation_legitimate_parent_attachment(): Property 2
    (Preservation). Uses seeded `random` to build several randomly-shaped
    node trees where every parent_id is either None or a node_id
    legitimately created earlier in the same execution, and asserts
    attachment + succeeded/failed counts match the observed baseline
    both before and after the fix.
"""

import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import asyncio
import inspect
import random
import uuid
from unittest.mock import AsyncMock, patch

import plan_executor
from design_plan import DesignNode, DesignPlan


def _fix_is_present() -> bool:
    """Detect whether execute_node's signature has been threaded with the
    created_node_ids allowlist parameter yet."""
    sig = inspect.signature(plan_executor.execute_node)
    return "created_node_ids" in sig.parameters


async def _mock_send_figma_command(action, payload):
    return {"status": "ok", "node_id": f"node-{uuid.uuid4().hex[:8]}"}


async def test_bug_condition_unrestricted_parent_id():
    """Property 1: Bug Condition -- Unrestricted Parent Targeting (C-2)."""
    print("Running test_bug_condition_unrestricted_parent_id...")

    node = DesignNode(type="rectangle", name="evil_rect", x=0, y=0, width=10, height=10)
    rogue_parent_id = "not-created-by-this-execution"

    with patch.object(plan_executor.bridge_client, "send_figma_command", new=AsyncMock(side_effect=_mock_send_figma_command)) as mocked:
        if _fix_is_present():
            result = await plan_executor.execute_node(node, parent_id=rogue_parent_id, created_node_ids=set())
            assert mocked.await_count == 0, (
                "Expected FIXED execute_node to never call bridge_client.send_figma_command "
                "for a parent_id outside the created-node allowlist."
            )
            assert result.get("status") == "error", (
                "Expected FIXED execute_node to return status='error' for a disallowed parent_id."
            )
            print("  [FIXED] confirmed: disallowed parent_id was rejected, send_figma_command never called.")
        else:
            result = await plan_executor.execute_node(node, parent_id=rogue_parent_id)
            assert mocked.await_count == 1, (
                "Expected UNFIXED execute_node to forward the arbitrary parent_id "
                "to bridge_client.send_figma_command unchecked (confirms C-2 bug)."
            )
            called_payload = mocked.await_args.args[1]
            assert called_payload.get("parent_id") == rogue_parent_id, (
                "Expected the rogue parent_id to be forwarded verbatim in the payload."
            )
            print(f"  [UNFIXED] confirmed: parent_id={rogue_parent_id!r} (never created this execution) "
                  "was forwarded to send_figma_command unchecked -- counterexample for C-2.")

    print("test_bug_condition_unrestricted_parent_id: PASSED\n")


async def _run_execute_plan_compat(plan):
    """Call execute_plan regardless of whether it now takes extra kwargs internally
    (it doesn't change signature per design.md, only execute_node/_execute_post_hoc_container do)."""
    return await plan_executor.execute_plan(plan)


def _random_node(rng, depth, max_depth=2):
    node_type = rng.choice(["rectangle", "text", "frame"])
    children = []
    if node_type == "frame" and depth < max_depth:
        for _ in range(rng.randint(0, 3)):
            children.append(_random_node(rng, depth + 1, max_depth))
    kwargs = dict(type=node_type, name=f"n_{rng.randint(0, 99999)}", x=rng.randint(0, 300),
                  y=rng.randint(0, 300), width=rng.randint(10, 200), height=rng.randint(10, 200))
    if node_type == "text":
        kwargs["content"] = f"content-{rng.randint(0, 999)}"
    if children:
        kwargs["children"] = children
    return DesignNode(**kwargs)


async def test_preservation_legitimate_parent_attachment():
    """Property 2: Preservation -- Legitimate Parent Attachment."""
    print("Running test_preservation_legitimate_parent_attachment...")

    rng = random.Random(1234)  # seeded for reproducibility

    with patch.object(plan_executor.bridge_client, "send_figma_command", new=AsyncMock(side_effect=_mock_send_figma_command)):
        for trial in range(5):
            elements = [_random_node(rng, depth=0) for _ in range(rng.randint(1, 3))]
            plan = DesignPlan(screen_name=f"trial-{trial}", elements=elements)
            result = await _run_execute_plan_compat(plan)

            assert result["failed"] == 0, (
                f"Trial {trial}: expected all nodes (using only None/legitimately-created "
                f"parent_id values) to succeed, got failed={result['failed']}, "
                f"results={result['results']}"
            )
            assert result["succeeded"] == result["total_nodes"], (
                f"Trial {trial}: succeeded ({result['succeeded']}) should equal "
                f"total_nodes ({result['total_nodes']}) for an all-legitimate tree."
            )
            for r in result["results"]:
                assert r.get("status") == "ok", f"Trial {trial}: unexpected non-ok result: {r}"

    print("  confirmed: randomly-shaped trees using only None/legitimately-created "
          "parent_id values attach every node and match expected succeeded/failed counts, "
          "on both pre-fix and post-fix code (since plan_executor never generates a "
          "non-allowlisted parent_id internally).")
    print("test_preservation_legitimate_parent_attachment: PASSED\n")


async def main():
    await test_bug_condition_unrestricted_parent_id()
    await test_preservation_legitimate_parent_attachment()
    print("All test_parent_allowlist checks completed.")


if __name__ == "__main__":
    asyncio.run(main())
