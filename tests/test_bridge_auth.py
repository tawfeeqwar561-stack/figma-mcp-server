"""
Tests for Subsystem 1 -- Bridge auth (C-1: unauthenticated bridge).

Run directly: python tests/test_bridge_auth.py

This file contains BOTH:
  - test_bug_condition_missing_token(): Property 1 (Bug Condition) --
    on UNFIXED code, proves that a role-identification message with no
    (or wrong) token is still accepted into _controllers/_plugins with
    no close() call. After the fix lands, this SAME function's
    assertions are flipped to assert the connection is rejected.
  - test_preservation_valid_token(): Property 2 (Preservation) --
    targets the POST-FIX message shape (role + token). It calls
    config.get_or_create_bridge_token() to build a valid handshake, so
    on UNFIXED code (before config.py gains that helper) it will raise
    an AttributeError -- this is expected and documented; the test can
    only fully pass once the fix (task 3) lands.

Uses a minimal FakeServerConnection (no real sockets) with unittest.mock
style manual stubbing, matching the existing tests/ convention (plain
assert-based scripts, no pytest/hypothesis dependency).
"""

import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import asyncio
import json

import websockets
from websockets.exceptions import ConnectionClosed

import bridge


class FakeServerConnection:
    """Minimal async stand-in for websockets.asyncio.server.ServerConnection."""

    def __init__(self, messages, remote_address=("127.0.0.1", 12345)):
        self._queue: asyncio.Queue = asyncio.Queue()
        for m in messages:
            self._queue.put_nowait(m)
        self.remote_address = remote_address
        self.sent: list[str] = []
        self.closed = False

    async def recv(self):
        item = await self._queue.get()
        if item is None:
            raise ConnectionClosed(None, None)
        return item

    async def send(self, message):
        self.sent.append(message)

    async def close(self):
        self.closed = True
        self._queue.put_nowait(None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self._queue.get()
        if item is None:
            raise StopAsyncIteration
        return item

    def push(self, message):
        self._queue.put_nowait(message)

    def end(self):
        self._queue.put_nowait(None)


async def _run_handle_connection_and_observe(fake_ws, settle=0.05):
    """
    Run bridge.handle_connection(fake_ws) as a background task and give it
    a moment to run past the initial handshake. If the connection is
    accepted, _handle_plugin/_handle_controller will block awaiting the
    next message (our fake queue is empty), so we can safely inspect
    bridge._controllers/_plugins mid-flight before cleaning up.
    """
    task = asyncio.ensure_future(bridge.handle_connection(fake_ws))
    await asyncio.sleep(settle)
    return task


async def _cleanup(task, fake_ws):
    fake_ws.end()
    try:
        await asyncio.wait_for(task, timeout=2.0)
    except asyncio.TimeoutError:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    finally:
        bridge._controllers.discard(fake_ws)
        bridge._plugins.discard(fake_ws)


async def test_bug_condition_missing_token():
    """Property 1: Bug Condition -- Unauthenticated Bridge Access (C-1)."""
    print("Running test_bug_condition_missing_token...")

    # --- controller role, no token ---
    fake_ws = FakeServerConnection([json.dumps({"role": "controller"})])
    task = await _run_handle_connection_and_observe(fake_ws)
    try:
        has_token_support = hasattr(bridge, "config") and hasattr(
            getattr(bridge, "config", None), "get_or_create_bridge_token"
        )
    except Exception:
        has_token_support = False

    if not has_token_support:
        # UNFIXED code: no auth check exists at all.
        assert fake_ws in bridge._controllers, (
            "Expected UNFIXED bridge to accept a token-less controller "
            "handshake into _controllers (confirms C-1 bug)."
        )
        assert fake_ws.closed is False, (
            "Expected UNFIXED bridge to never close a token-less controller connection."
        )
        print("  [UNFIXED] confirmed: {'role': 'controller'} with no token was accepted "
              "into _controllers and never closed -- counterexample for C-1.")
    else:
        # FIXED code: missing token must be rejected.
        assert fake_ws not in bridge._controllers, (
            "Expected FIXED bridge to reject a token-less controller handshake."
        )
        assert fake_ws.closed is True, (
            "Expected FIXED bridge to close a token-less controller connection."
        )
        assert any("error" in s for s in fake_ws.sent), (
            "Expected FIXED bridge to send a structured error before closing."
        )
        print("  [FIXED] confirmed: {'role': 'controller'} with no token was rejected and closed.")
    await _cleanup(task, fake_ws)

    # --- plugin role, wrong token ---
    fake_ws2 = FakeServerConnection([json.dumps({"role": "plugin", "token": "wrong-token-value"})])
    task2 = await _run_handle_connection_and_observe(fake_ws2)
    if not has_token_support:
        assert fake_ws2 in bridge._plugins, (
            "Expected UNFIXED bridge to accept a plugin handshake with an arbitrary token value."
        )
        assert fake_ws2.closed is False
        print("  [UNFIXED] confirmed: {'role': 'plugin', 'token': 'wrong-token-value'} was accepted "
              "into _plugins -- counterexample for C-1.")
    else:
        assert fake_ws2 not in bridge._plugins, (
            "Expected FIXED bridge to reject a plugin handshake with a wrong token."
        )
        assert fake_ws2.closed is True
        print("  [FIXED] confirmed: wrong-token plugin handshake was rejected and closed.")
    await _cleanup(task2, fake_ws2)

    print("test_bug_condition_missing_token: PASSED\n")


async def test_preservation_valid_token():
    """Property 2: Preservation -- Authenticated Role Handshake."""
    print("Running test_preservation_valid_token...")

    import config

    if not hasattr(config, "get_or_create_bridge_token"):
        print("  [SKIPPED on UNFIXED code] config.get_or_create_bridge_token() does not "
              "exist yet -- this preservation test targets the POST-FIX handshake shape "
              "and can only fully run once the C-1 fix (task 3) is implemented.")
        return

    token = config.get_or_create_bridge_token()

    # Controller presents the correct token -> accepted and dispatched.
    controller_ws = FakeServerConnection([json.dumps({"role": "controller", "token": token})])
    c_task = await _run_handle_connection_and_observe(controller_ws)
    assert controller_ws in bridge._controllers, (
        "A correct-token controller handshake must still be accepted (Preservation 3.1)."
    )
    assert controller_ws.closed is False

    # Now exercise the existing relay + result-routing round trip:
    # controller sends a command with a request_id, a plugin (also
    # correctly authenticated) receives it via relay, and its result is
    # routed back to the controller.
    plugin_ws = FakeServerConnection([json.dumps({"role": "plugin", "token": token})])
    p_task = await _run_handle_connection_and_observe(plugin_ws)
    assert plugin_ws in bridge._plugins, (
        "A correct-token plugin handshake must still be accepted (Preservation 3.2)."
    )

    command = {"request_id": "req-1", "action": "create_rectangle", "payload": {"x": 0}}
    controller_ws.push(json.dumps(command))
    await asyncio.sleep(0.05)
    assert any(json.loads(m).get("action") == "create_rectangle" for m in plugin_ws.sent), (
        "Command from controller must still be relayed to the plugin (Preservation 3.5)."
    )

    plugin_result = {"request_id": "req-1", "status": "ok", "node_id": "abc123"}
    plugin_ws.push(json.dumps(plugin_result))
    await asyncio.sleep(0.05)
    assert any(json.loads(m).get("node_id") == "abc123" for m in controller_ws.sent), (
        "Plugin result must still be routed back to the originating controller "
        "via request_id (Preservation 3.2/3.6)."
    )

    await _cleanup(c_task, controller_ws)
    await _cleanup(p_task, plugin_ws)

    print("  [FIXED] confirmed: correct-token handshake + relay + result routing round trip "
          "behaves exactly like the pre-fix baseline.")
    print("test_preservation_valid_token: PASSED\n")


async def main():
    await test_bug_condition_missing_token()
    await test_preservation_valid_token()
    print("All test_bridge_auth checks completed.")


if __name__ == "__main__":
    asyncio.run(main())
