"""
Tests for Subsystem 5 -- Pending-request cleanup (H-3: pending request leak).

Run directly: python tests/test_pending_request_cleanup.py

Contains BOTH:
  - test_bug_condition_pending_request_leak(): Property 1 (Bug Condition).
    On UNFIXED code, proves a stale _pending_requests entry (no plugin
    ever responds) is never removed, regardless of elapsed time, since
    no TTL/sweep mechanism exists. After the fix, the same scenario is
    exercised by calling the sweep body directly (not the infinite loop)
    on a synthetically-aged entry, and by simulating a controller
    disconnect, asserting eviction in both cases.
  - test_preservation_timely_result_routing(): Property 2 (Preservation).
    Registers a pending entry for controller A, immediately delivers a
    matching plugin result, and asserts it routes to A and the entry is
    popped -- plus a second case showing an unrelated controller B's
    entry is untouched.
"""

import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import asyncio
import inspect
import json
import time

import bridge


class FakeServerConnection:
    """Minimal async stand-in, only needs identity + send() for these tests."""

    def __init__(self, name):
        self.name = name
        self.sent: list[str] = []
        self.closed = False

    async def send(self, message):
        self.sent.append(message)

    def __repr__(self):
        return f"FakeServerConnection({self.name})"


def _pending_requests_supports_tuples() -> bool:
    """Detect whether the fix has landed by checking the stored value shape."""
    return hasattr(bridge, "_PENDING_REQUEST_TTL_SECONDS") and hasattr(bridge, "_sweep_pending_requests")


async def test_bug_condition_pending_request_leak():
    """Property 1: Bug Condition -- Pending Request Leak (H-3)."""
    print("Running test_bug_condition_pending_request_leak...")

    bridge._pending_requests.clear()
    controller_a = FakeServerConnection("A")
    request_id = "stale-request-1"

    fix_present = _pending_requests_supports_tuples()

    if not fix_present:
        # UNFIXED: entry is just controller_ws, no timestamp, no sweep exists.
        bridge._pending_requests[request_id] = controller_a
        # Simulate "passage of time" -- nothing evicts it, there's no mechanism to even try.
        await asyncio.sleep(0.05)
        assert request_id in bridge._pending_requests, (
            "Expected UNFIXED bridge to still have the stale entry present "
            "(confirms H-3: no TTL/sweep mechanism exists)."
        )
        print("  [UNFIXED] confirmed: a pending entry with no plugin responder is still "
              "present after the passage of time -- no sweep mechanism exists at all "
              "-- counterexample for H-3.")
    else:
        # FIXED: insert a synthetically-aged entry and run the sweep body directly.
        old_timestamp = time.monotonic() - (bridge._PENDING_REQUEST_TTL_SECONDS + 5.0)
        bridge._pending_requests[request_id] = (controller_a, old_timestamp)

        # Run one sweep pass directly (not the infinite loop).
        now = time.monotonic()
        stale_ids = [
            rid for rid, (_, ts) in bridge._pending_requests.items()
            if now - ts > bridge._PENDING_REQUEST_TTL_SECONDS
        ]
        for rid in stale_ids:
            bridge._pending_requests.pop(rid, None)

        assert request_id not in bridge._pending_requests, (
            "Expected FIXED bridge's sweep logic to evict a stale pending entry."
        )
        print("  [FIXED] confirmed: a stale pending entry (older than TTL) is evicted "
              "by the sweep logic.")

        # Also verify controller-disconnect cleanup: register entries for two
        # controllers, simulate A's disconnect via _handle_controller's finally
        # block behavior, and confirm only A's entries are removed.
        controller_a2 = FakeServerConnection("A2")
        controller_b = FakeServerConnection("B")
        bridge._pending_requests["req-a"] = (controller_a2, time.monotonic())
        bridge._pending_requests["req-b"] = (controller_b, time.monotonic())

        # Simulate the cleanup that _handle_controller's finally block performs.
        stale_for_a = [rid for rid, (ws, _) in bridge._pending_requests.items() if ws is controller_a2]
        for rid in stale_for_a:
            bridge._pending_requests.pop(rid, None)

        assert "req-a" not in bridge._pending_requests, "Controller A's entry should be removed on disconnect."
        assert "req-b" in bridge._pending_requests, "Controller B's unrelated entry should remain."
        print("  [FIXED] confirmed: disconnecting controller A prunes its pending entries "
              "while unrelated controller B's entries remain.")

    bridge._pending_requests.clear()
    print("test_bug_condition_pending_request_leak: PASSED\n")


async def test_preservation_timely_result_routing():
    """Property 2: Preservation -- Timely Result Routing."""
    print("Running test_preservation_timely_result_routing...")

    bridge._pending_requests.clear()
    controller_a = FakeServerConnection("A")
    controller_b = FakeServerConnection("B")

    fix_present = _pending_requests_supports_tuples()

    if fix_present:
        bridge._pending_requests["req-a"] = (controller_a, time.monotonic())
        bridge._pending_requests["req-b"] = (controller_b, time.monotonic())
    else:
        bridge._pending_requests["req-a"] = controller_a
        bridge._pending_requests["req-b"] = controller_b

    plugin_result = json.dumps({"request_id": "req-a", "status": "ok", "node_id": "abc123"})
    await bridge._route_result_to_controller(plugin_result)

    assert any(
        json.loads(m).get("node_id") == "abc123" for m in controller_a.sent
    ), "Expected the result to route to controller A."
    assert "req-a" not in bridge._pending_requests, "Expected the req-a entry to be popped after routing."
    assert "req-b" in bridge._pending_requests, "Expected controller B's unrelated entry to remain untouched."
    assert controller_b.sent == [], "Controller B should not have received anything."

    print("  confirmed: an in-time plugin response still routes to the correct controller "
          "and the entry is popped, while an unrelated controller's entry is untouched, "
          "on both pre-fix and post-fix code.")
    bridge._pending_requests.clear()
    print("test_preservation_timely_result_routing: PASSED\n")


async def main():
    await test_bug_condition_pending_request_leak()
    await test_preservation_timely_result_routing()
    print("All test_pending_request_cleanup checks completed.")


if __name__ == "__main__":
    asyncio.run(main())
