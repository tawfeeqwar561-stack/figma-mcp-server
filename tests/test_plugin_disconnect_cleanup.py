"""
Tests for graceful plugin disconnect handling (Phase 1 reliability
follow-up to bridge-security-hardening's H-3 pending-request TTL sweep).

Context: before this fix, if the plugin holding an in-flight command died
mid-request, the controller's OWN response timeout (bridge_client.py's
BRIDGE_RESPONSE_TIMEOUT_SECONDS, 10s) or, failing that, the bridge's TTL
sweep (20s) was the only thing that would ever notice -- there was no
proactive, immediate signal from the disconnect itself. This mirrors the
"no plugin connected" immediate-error path _handle_controller already
has for a BRAND NEW request (see H-3/bridge.py), extended to requests
that are already in flight when the last plugin disconnects.

Run directly: python tests/test_plugin_disconnect_cleanup.py

Contains:
  - test_last_plugin_disconnect_fails_pending_immediately(): registers a
    pending request tied to a fake plugin connection, simulates that
    plugin's _handle_plugin loop ending (ConnectionClosed), and asserts
    the owning controller receives an immediate structured error and the
    entry is removed -- without waiting for any timeout/sweep.
  - test_pending_survives_if_another_plugin_remains(): with two plugins
    connected, one disconnecting must NOT fail requests tied to the
    other's still-viable connection (a pending request isn't actually
    tied to a specific plugin instance -- _relay_to_plugins broadcasts to
    all of them -- so only an all-plugins-gone disconnect should trigger
    the fast-fail path).
  - test_preservation_normal_routing_unaffected(): a plugin that responds
    normally (no disconnect involved) still routes results exactly as
    before; this fix only adds a new failure-path behavior, it does not
    change the success path.
"""

import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import asyncio
import json
import time

import bridge


class FakeServerConnection:
    """Minimal async stand-in: identity + send() + an async-iterable queue
    that raises websockets' ConnectionClosed when ended, matching the
    convention already used by test_bridge_auth.py / test_malformed_controller_input.py."""

    def __init__(self, name):
        self.name = name
        self.sent: list[str] = []
        self.remote_address = ("127.0.0.1", 0)
        self._queue: asyncio.Queue = asyncio.Queue()

    async def send(self, message):
        self.sent.append(message)

    def push(self, message):
        self._queue.put_nowait(message)

    def end(self):
        self._queue.put_nowait(None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self._queue.get()
        if item is None:
            raise bridge.websockets.exceptions.ConnectionClosed(None, None)
        return item

    def __repr__(self):
        return f"FakeServerConnection({self.name})"


async def test_last_plugin_disconnect_fails_pending_immediately():
    print("Running test_last_plugin_disconnect_fails_pending_immediately...")

    bridge._pending_requests.clear()
    bridge._plugins.clear()

    plugin_ws = FakeServerConnection("plugin-1")
    controller_ws = FakeServerConnection("controller-1")

    bridge._plugins.add(plugin_ws)
    bridge._pending_requests["req-1"] = (controller_ws, time.monotonic())

    plugin_ws.end()  # simulate the plugin's socket closing
    started = time.monotonic()
    await asyncio.wait_for(bridge._handle_plugin(plugin_ws), timeout=2.0)
    elapsed = time.monotonic() - started

    assert plugin_ws not in bridge._plugins, "Expected the disconnected plugin to be removed from _plugins."
    assert "req-1" not in bridge._pending_requests, "Expected the pending entry to be removed immediately, not left for a sweep/timeout."
    assert len(controller_ws.sent) == 1, f"Expected exactly one message sent to the controller, got {controller_ws.sent!r}"
    sent = json.loads(controller_ws.sent[0])
    assert sent.get("request_id") == "req-1"
    assert sent.get("status") == "error"
    assert "disconnect" in sent.get("message", "").lower()
    assert elapsed < 1.0, f"Expected immediate failure, took {elapsed:.2f}s."

    print(f"  confirmed: when the last plugin disconnects, a pending request tied to it "
          f"is failed immediately ({elapsed:.3f}s) with a clear disconnect message, "
          f"instead of waiting for a response timeout or TTL sweep.")
    print("test_last_plugin_disconnect_fails_pending_immediately: PASSED\n")

    bridge._pending_requests.clear()
    bridge._plugins.clear()


async def test_pending_survives_if_another_plugin_remains():
    print("Running test_pending_survives_if_another_plugin_remains...")

    bridge._pending_requests.clear()
    bridge._plugins.clear()

    plugin_a = FakeServerConnection("plugin-a")
    plugin_b = FakeServerConnection("plugin-b")
    controller_ws = FakeServerConnection("controller-1")

    bridge._plugins.add(plugin_a)
    bridge._plugins.add(plugin_b)
    bridge._pending_requests["req-2"] = (controller_ws, time.monotonic())

    plugin_a.end()
    await asyncio.wait_for(bridge._handle_plugin(plugin_a), timeout=2.0)

    assert plugin_a not in bridge._plugins
    assert plugin_b in bridge._plugins, "The still-connected plugin must remain in _plugins."
    assert "req-2" in bridge._pending_requests, (
        "Expected the pending entry to SURVIVE plugin_a's disconnect, since plugin_b "
        "(which every command is broadcast to) is still connected and could still answer it."
    )
    assert controller_ws.sent == [], "Expected no premature error sent to the controller."

    print("  confirmed: one plugin disconnecting while another remains connected does "
          "NOT fail pending requests -- only losing every plugin triggers the fast-fail path.")
    print("test_pending_survives_if_another_plugin_remains: PASSED\n")

    bridge._pending_requests.clear()
    bridge._plugins.clear()


async def test_preservation_normal_routing_unaffected():
    print("Running test_preservation_normal_routing_unaffected...")

    bridge._pending_requests.clear()
    bridge._plugins.clear()

    plugin_ws = FakeServerConnection("plugin-1")
    controller_ws = FakeServerConnection("controller-1")
    bridge._plugins.add(plugin_ws)
    bridge._pending_requests["req-3"] = (controller_ws, time.monotonic())

    plugin_ws.push(json.dumps({"request_id": "req-3", "status": "ok", "node_id": "abc123"}))
    plugin_ws.end()

    await asyncio.wait_for(bridge._handle_plugin(plugin_ws), timeout=2.0)

    assert len(controller_ws.sent) == 1, f"Expected exactly one message (the real result), got {controller_ws.sent!r}"
    sent = json.loads(controller_ws.sent[0])
    assert sent.get("status") == "ok" and sent.get("node_id") == "abc123", (
        f"Expected the plugin's real result to route through unchanged, got {sent!r}"
    )

    print("  confirmed: a plugin that answers before disconnecting still routes its real "
          "result normally -- this fix only changes what happens to requests left unanswered.")
    print("test_preservation_normal_routing_unaffected: PASSED\n")

    bridge._pending_requests.clear()
    bridge._plugins.clear()


async def main():
    await test_last_plugin_disconnect_fails_pending_immediately()
    await test_pending_survives_if_another_plugin_remains()
    await test_preservation_normal_routing_unaffected()
    print("All test_plugin_disconnect_cleanup checks completed.")


if __name__ == "__main__":
    asyncio.run(main())
