"""
Tests for the persistent-connection reliability layer in bridge_client.py
(connection reuse, bounded reconnect/backoff, concurrent request_id
correlation, pending-request cleanup, graceful shutdown), plus preservation
checks that this rewrite does not regress bridge-security-hardening's
auth/allowlist/timeout guarantees or existing plan execution.

Run directly: python tests/test_bridge_connection_reliability.py

Two harnesses are used:
  - _FakeBridgeServer: a minimal hand-rolled stand-in for bridge.py, used
    for the pure connection-lifecycle/retry/concurrency scenarios where we
    need full control over server-side behavior (delay responses, drop
    connections on demand, reply out of order) that the real bridge.py
    doesn't expose hooks for. Runs on TEST_PORT (19765).
  - A real bridge.start_bridge() on ALT_PORT (18766), mirroring
    tests/test_full_regression_e2e.py's pattern, for the scenarios that need
    the real wire protocol end to end: plugin-unavailable, authentication
    preservation, and existing plan-execution preservation.

Each test swaps bridge_client's module-level connection singleton for a
fresh, isolated _BridgeConnection for its duration (see _isolated_connection
below), so tests never leak open sockets or pending futures into each other.

Follows the existing tests/ convention: standalone script, stdlib
unittest.mock, no pytest, async def main() + asyncio.run(main()), printed
PASSED markers.
"""

import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import asyncio
import json
import time
import uuid
from contextlib import asynccontextmanager
from unittest.mock import patch

import websockets
from websockets.exceptions import ConnectionClosed

import bridge
import bridge_client
import config
import planner
from plan_executor import execute_plan

TEST_PORT = 19765
ALT_PORT = 18766


@asynccontextmanager
async def _isolated_connection():
    """
    Swap in a fresh _BridgeConnection for the duration of a test so tests
    don't leak connection state (open sockets, pending futures) into each
    other via the shared module-level singleton -- while still exercising
    the real public functions (send_figma_command/close/is_connected),
    which is what other modules (tools.py, plan_executor.py) actually call.
    """
    original = bridge_client._connection
    fresh = bridge_client._BridgeConnection()
    bridge_client._connection = fresh
    try:
        yield fresh
    finally:
        await fresh.close()
        bridge_client._connection = original


@asynccontextmanager
async def _fast_retry_config(max_attempts=3, base_delay=0.02, max_delay=0.05, response_timeout=None):
    """Patch the retry/backoff/timeout knobs to small values so tests run
    fast, without changing the *behavior* under test (still bounded, still
    exponential, just on a compressed timescale)."""
    patches = [
        patch.object(config, "BRIDGE_CONNECT_MAX_ATTEMPTS", max_attempts),
        patch.object(config, "BRIDGE_RECONNECT_BASE_DELAY_SECONDS", base_delay),
        patch.object(config, "BRIDGE_RECONNECT_MAX_DELAY_SECONDS", max_delay),
    ]
    if response_timeout is not None:
        patches.append(patch.object(config, "BRIDGE_RESPONSE_TIMEOUT_SECONDS", response_timeout))
    for p in patches:
        p.start()
    try:
        yield
    finally:
        for p in reversed(patches):
            p.stop()


class _FakeBridgeServer:
    """
    A minimal, fully-scriptable stand-in for bridge.py's controller-facing
    protocol, used only to drive bridge_client.py's connection-management
    logic under precise, deterministic conditions.
    """

    def __init__(self, token: str, port: int):
        self.token = token
        self.port = port
        self.connections_seen = 0
        self.received_actions: list[str] = []
        self._server = None
        # Behavior knobs, settable by tests before/while connected:
        self.respond = True
        self.response_delay = 0.0
        self.reverse_response_order = False
        self._buffered: list[tuple] = []  # (ws, request_id) pairs, if buffering for reverse order

    async def _handler(self, ws) -> None:
        self.connections_seen += 1
        try:
            first = json.loads(await ws.recv())
            if first.get("token") != self.token:
                await ws.send(json.dumps({"status": "error", "message": "Invalid or missing bridge token"}))
                await ws.close()
                return

            async for raw in ws:
                cmd = json.loads(raw)
                self.received_actions.append(cmd.get("action"))
                if not self.respond:
                    continue
                if self.reverse_response_order:
                    self._buffered.append((ws, cmd))
                    continue
                if self.response_delay:
                    await asyncio.sleep(self.response_delay)
                await ws.send(json.dumps({
                    "request_id": cmd.get("request_id"), "status": "ok",
                    "node_id": f"node-{cmd.get('action')}",
                }))

            # Connection ended (client closed its send side) -- flush any
            # buffered reversed responses before returning.
            for buffered_ws, cmd in reversed(self._buffered):
                try:
                    await buffered_ws.send(json.dumps({
                        "request_id": cmd.get("request_id"), "status": "ok",
                        "node_id": f"node-{cmd.get('action')}",
                    }))
                except ConnectionClosed:
                    pass
            self._buffered.clear()
        except ConnectionClosed:
            pass

    async def flush_reversed(self) -> None:
        """Send all buffered responses in reverse order of receipt (used by
        the request_id-isolation test to prove correlation isn't positional)."""
        for buffered_ws, cmd in reversed(self._buffered):
            await buffered_ws.send(json.dumps({
                "request_id": cmd.get("request_id"), "status": "ok",
                "node_id": f"node-{cmd.get('action')}",
            }))
        self._buffered.clear()

    async def start(self) -> None:
        self._server = await websockets.serve(self._handler, "localhost", self.port)

    async def stop(self) -> None:
        self._server.close()
        await self._server.wait_closed()


async def test_initial_connection_and_successful_command():
    """Scenario 1 + 2: initial connection, and a successful command round trip."""
    print("Running test_initial_connection_and_successful_command...")

    token = "test-token-1"
    server = _FakeBridgeServer(token, TEST_PORT)
    await server.start()
    try:
        async with _isolated_connection():
            with patch.object(config, "BRIDGE_CLIENT_URI", f"ws://localhost:{TEST_PORT}"), \
                 patch.object(config, "BRIDGE_AUTH_TOKEN", token):
                assert bridge_client.is_connected() is False, "Should not be connected before any call."
                result = await bridge_client.send_figma_command("create_rectangle", {"x": 1})
                assert result == {"request_id": result["request_id"], "status": "ok", "node_id": "node-create_rectangle"}
                assert bridge_client.is_connected() is True, "Should be connected after a successful call."
                assert server.connections_seen == 1, "Expected exactly one physical connection for one command."
    finally:
        await server.stop()

    print("  confirmed: first send_figma_command call establishes the connection and completes "
          "successfully; is_connected() reflects state correctly before/after.")
    print("test_initial_connection_and_successful_command: PASSED\n")


async def test_connection_is_reused_across_calls():
    """Persistence check: multiple sequential calls share one physical connection."""
    print("Running test_connection_is_reused_across_calls...")

    token = "test-token-2"
    server = _FakeBridgeServer(token, TEST_PORT + 1)
    await server.start()
    try:
        async with _isolated_connection():
            with patch.object(config, "BRIDGE_CLIENT_URI", f"ws://localhost:{TEST_PORT + 1}"), \
                 patch.object(config, "BRIDGE_AUTH_TOKEN", token):
                for _ in range(5):
                    await bridge_client.send_figma_command("create_text", {})
                assert server.connections_seen == 1, (
                    f"Expected 5 sequential commands to reuse a single connection, "
                    f"got {server.connections_seen} separate connections."
                )
    finally:
        await server.stop()

    print("  confirmed: 5 sequential send_figma_command calls reused a single physical "
          "connection instead of opening a new socket per command.")
    print("test_connection_is_reused_across_calls: PASSED\n")


async def test_bridge_disconnect_detected():
    """Scenario 3: bridge disconnect -- dropping mid-session is detected cleanly, not hung."""
    print("Running test_bridge_disconnect_detected...")

    token = "test-token-3"
    server = _FakeBridgeServer(token, TEST_PORT + 2)
    await server.start()

    async with _fast_retry_config(max_attempts=2), _isolated_connection():
        with patch.object(config, "BRIDGE_CLIENT_URI", f"ws://localhost:{TEST_PORT + 2}"), \
             patch.object(config, "BRIDGE_AUTH_TOKEN", token):
            await bridge_client.send_figma_command("create_rectangle", {})
            assert bridge_client.is_connected() is True

            await server.stop()  # simulate the bridge going away mid-session
            await asyncio.sleep(0.1)  # let the reader loop observe the close

            raised = None
            try:
                await bridge_client.send_figma_command("create_text", {})
            except ConnectionError as exc:
                raised = exc
            assert raised is not None, (
                "Expected a clear ConnectionError once the bridge is gone, not a hang or a fabricated success."
            )
            assert "bridge" in str(raised).lower()

    print("  confirmed: after the bridge disconnects, the next command fails fast with a "
          "clear ConnectionError instead of hanging.")
    print("test_bridge_disconnect_detected: PASSED\n")


async def test_reconnect_after_bridge_restart():
    """Scenario 4: reconnect -- a fresh bridge coming back on the same address is used transparently."""
    print("Running test_reconnect_after_bridge_restart...")

    token = "test-token-4"
    port = TEST_PORT + 3
    server = _FakeBridgeServer(token, port)
    await server.start()

    async with _fast_retry_config(), _isolated_connection():
        with patch.object(config, "BRIDGE_CLIENT_URI", f"ws://localhost:{port}"), \
             patch.object(config, "BRIDGE_AUTH_TOKEN", token):
            await bridge_client.send_figma_command("create_rectangle", {})
            await server.stop()
            await asyncio.sleep(0.1)

            new_server = _FakeBridgeServer(token, port)
            await new_server.start()
            try:
                result = await bridge_client.send_figma_command("create_text", {})
                assert result.get("status") == "ok", f"Expected reconnect + successful retry, got {result}"
                assert new_server.connections_seen == 1, "Expected exactly one reconnect to the restarted bridge."
            finally:
                await new_server.stop()

    print("  confirmed: once the bridge comes back, the next command transparently "
          "reconnects and succeeds with no manual intervention.")
    print("test_reconnect_after_bridge_restart: PASSED\n")


async def test_bounded_retry_terminates():
    """Scenario 5: bounded retry -- connect failures are retried a bounded number of times, never forever."""
    print("Running test_bounded_retry_terminates...")

    attempt_counter = {"n": 0}

    async def _always_refuse(*_args, **_kwargs):
        attempt_counter["n"] += 1
        raise OSError("connection refused (simulated)")

    async with _fast_retry_config(max_attempts=3, base_delay=0.01, max_delay=0.02), _isolated_connection():
        with patch.object(bridge_client.websockets, "connect", side_effect=_always_refuse):
            started = time.monotonic()
            raised = None
            try:
                await bridge_client.send_figma_command("create_rectangle", {})
            except ConnectionError as exc:
                raised = exc
            elapsed = time.monotonic() - started

    assert raised is not None, "Expected send_figma_command to raise ConnectionError, not hang or succeed."
    assert attempt_counter["n"] == config.BRIDGE_CONNECT_MAX_ATTEMPTS or attempt_counter["n"] == 3, (
        f"Expected exactly the configured bounded number of connect attempts, got {attempt_counter['n']}."
    )
    assert elapsed < 2.0, f"Expected bounded retry to terminate quickly with small backoff config, took {elapsed:.2f}s."

    print(f"  confirmed: retry stopped after exactly {attempt_counter['n']} attempts "
          f"(bounded, not infinite), raised ConnectionError, total elapsed {elapsed:.3f}s.")
    print("test_bounded_retry_terminates: PASSED\n")


async def test_concurrent_requests_no_cross_talk():
    """Scenario 6: concurrent requests -- multiple in-flight commands on one shared
    connection each get their own correct response, with no cross-talk."""
    print("Running test_concurrent_requests_no_cross_talk...")

    token = "test-token-6"
    port = TEST_PORT + 4
    server = _FakeBridgeServer(token, port)
    server.response_delay = 0.05  # ensure overlap: all N requests are in flight at once
    await server.start()

    actions = ["create_rectangle", "create_text", "create_frame", "create_ellipse", "create_line"]
    try:
        async with _isolated_connection():
            with patch.object(config, "BRIDGE_CLIENT_URI", f"ws://localhost:{port}"), \
                 patch.object(config, "BRIDGE_AUTH_TOKEN", token):
                results = await asyncio.gather(*[
                    bridge_client.send_figma_command(action, {}) for action in actions
                ])
                for action, result in zip(actions, results):
                    assert result.get("node_id") == f"node-{action}", (
                        f"Expected {action}'s own response, got {result} -- possible cross-talk."
                    )
                assert server.connections_seen == 1, "Expected all concurrent requests to share one connection."
                assert len(server.received_actions) == len(actions)
    finally:
        await server.stop()

    print(f"  confirmed: {len(actions)} concurrent send_figma_command calls over one shared "
          "connection each received their own matching response, no cross-talk.")
    print("test_concurrent_requests_no_cross_talk: PASSED\n")


async def test_request_id_isolation_out_of_order_responses():
    """Scenario 7: request_id isolation -- correlation is by request_id, not by
    the order responses arrive on the wire."""
    print("Running test_request_id_isolation_out_of_order_responses...")

    token = "test-token-7"
    port = TEST_PORT + 5
    server = _FakeBridgeServer(token, port)
    server.reverse_response_order = True
    await server.start()

    actions = ["create_rectangle", "create_text", "create_frame"]
    try:
        async with _isolated_connection():
            with patch.object(config, "BRIDGE_CLIENT_URI", f"ws://localhost:{port}"), \
                 patch.object(config, "BRIDGE_AUTH_TOKEN", token):
                tasks = [asyncio.ensure_future(bridge_client.send_figma_command(a, {})) for a in actions]
                # Give the server a moment to receive and buffer all three
                # commands before it flushes responses in REVERSE order.
                await asyncio.sleep(0.1)
                await server.flush_reversed()
                results = await asyncio.gather(*tasks)
                for action, result in zip(actions, results):
                    assert result.get("node_id") == f"node-{action}", (
                        f"Expected {action}'s own response even though the server replied "
                        f"out of order, got {result}."
                    )
    finally:
        await server.stop()

    print("  confirmed: even when the server sends responses in reverse order of receipt, "
          "each caller's send_figma_command still gets matched to ITS OWN request_id "
          "correctly -- correlation is not positional.")
    print("test_request_id_isolation_out_of_order_responses: PASSED\n")


async def test_pending_request_cleanup_on_every_exit_path():
    """Scenario 8: pending request cleanup -- the internal pending-futures table
    never leaks an entry, on success, timeout, or connection-error exit paths."""
    print("Running test_pending_request_cleanup_on_every_exit_path...")

    token = "test-token-8"
    port = TEST_PORT + 6

    # --- success path ---
    server = _FakeBridgeServer(token, port)
    await server.start()
    try:
        async with _isolated_connection() as conn:
            with patch.object(config, "BRIDGE_CLIENT_URI", f"ws://localhost:{port}"), \
                 patch.object(config, "BRIDGE_AUTH_TOKEN", token):
                await bridge_client.send_figma_command("create_rectangle", {})
                assert conn._pending == {}, f"Expected no leaked pending entries after success, got {conn._pending}"
    finally:
        await server.stop()

    # --- timeout path (server never responds) ---
    server2 = _FakeBridgeServer(token, port)
    server2.respond = False
    await server2.start()
    try:
        async with _fast_retry_config(response_timeout=0.1), _isolated_connection() as conn2:
            with patch.object(config, "BRIDGE_CLIENT_URI", f"ws://localhost:{port}"), \
                 patch.object(config, "BRIDGE_AUTH_TOKEN", token):
                try:
                    await bridge_client.send_figma_command("create_text", {})
                    assert False, "Expected a TimeoutError."
                except TimeoutError:
                    pass
                assert conn2._pending == {}, f"Expected no leaked pending entries after timeout, got {conn2._pending}"
    finally:
        await server2.stop()

    # --- connection-error path (nobody listening) ---
    async def _always_refuse(*_a, **_kw):
        raise OSError("simulated refusal")

    async with _fast_retry_config(max_attempts=1, base_delay=0.01, max_delay=0.01), _isolated_connection() as conn3:
        with patch.object(bridge_client.websockets, "connect", side_effect=_always_refuse):
            try:
                await bridge_client.send_figma_command("create_frame", {})
                assert False, "Expected a ConnectionError."
            except ConnectionError:
                pass
            assert conn3._pending == {}, f"Expected no leaked pending entries after connection error, got {conn3._pending}"

    print("  confirmed: the pending-request table is empty after success, after timeout, "
          "and after a connection error -- no leak on any exit path.")
    print("test_pending_request_cleanup_on_every_exit_path: PASSED\n")


async def test_graceful_shutdown():
    """Scenario 10: graceful shutdown -- close() cancels the reader, fails anything
    still pending promptly, and leaves the connection in a clean, inert state."""
    print("Running test_graceful_shutdown...")

    token = "test-token-10"
    port = TEST_PORT + 7
    server = _FakeBridgeServer(token, port)
    server.respond = False  # so the in-flight call is still pending when we shut down
    await server.start()

    try:
        async with _isolated_connection() as conn:
            with patch.object(config, "BRIDGE_CLIENT_URI", f"ws://localhost:{port}"), \
                 patch.object(config, "BRIDGE_AUTH_TOKEN", token):
                in_flight = asyncio.ensure_future(bridge_client.send_figma_command("create_rectangle", {}))
                await asyncio.sleep(0.1)  # let it connect and register as pending

                started = time.monotonic()
                await bridge_client.close()
                elapsed = time.monotonic() - started

                raised = None
                try:
                    await in_flight
                except ConnectionError as exc:
                    raised = exc
                assert raised is not None, "Expected the in-flight call to be failed promptly by close(), not hang."
                assert elapsed < 1.0, f"Expected close() to return quickly, took {elapsed:.2f}s."
                assert bridge_client.is_connected() is False
                assert conn._pending == {}, "Expected no leaked pending entries after shutdown."

                # A further send after close() must fail cleanly, not hang or crash oddly.
                try:
                    await bridge_client.send_figma_command("create_text", {})
                    assert False, "Expected send after close() to raise, not succeed."
                except ConnectionError:
                    pass
    finally:
        await server.stop()

    print("  confirmed: close() promptly fails an in-flight request, leaves is_connected() "
          "False, clears pending state, and further use after shutdown fails cleanly.")
    print("test_graceful_shutdown: PASSED\n")


async def _fake_plugin_session(token, port, ready_event, stop_event):
    """Mirrors tests/test_full_regression_e2e.py's fake plugin harness."""
    uri = f"ws://localhost:{port}"
    async with websockets.connect(uri) as ws:
        await ws.send(json.dumps({"role": "plugin", "token": token}))
        ready_event.set()
        while not stop_event.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            command = json.loads(raw)
            result = {"request_id": command.get("request_id"), "status": "ok", "node_id": "e2e-node"}
            await ws.send(json.dumps(result))


async def test_plugin_unavailable_gives_clear_fast_error():
    """Scenario 9: plugin unavailable -- a real bridge with zero plugins connected
    must report this clearly and quickly, not via a generic 10s response timeout."""
    print("Running test_plugin_unavailable_gives_clear_fast_error...")

    token = config.get_or_create_bridge_token()
    server_task = asyncio.ensure_future(bridge.start_bridge(host="localhost", port=ALT_PORT))
    await asyncio.sleep(0.2)

    try:
        async with _isolated_connection():
            with patch.object(config, "BRIDGE_CLIENT_URI", f"ws://localhost:{ALT_PORT}"):
                started = time.monotonic()
                result = await bridge_client.send_figma_command("create_rectangle", {"x": 0})
                elapsed = time.monotonic() - started

        assert result.get("status") == "error", f"Expected a clear error result, got {result}"
        assert "plugin" in result.get("message", "").lower(), f"Expected a plugin-unavailable message, got {result}"
        assert elapsed < 2.0, (
            f"Expected an immediate plugin-unavailable error (not the ~10s response timeout), took {elapsed:.2f}s."
        )
    finally:
        server_task.cancel()
        try:
            await server_task
        except (asyncio.CancelledError, Exception):
            pass

    print(f"  confirmed: with zero plugins connected to a real bridge, the command fails "
          f"fast ({elapsed:.3f}s) with a clear 'no plugin connected' message, "
          f"instead of the ~10s generic response timeout.")
    print("test_plugin_unavailable_gives_clear_fast_error: PASSED\n")


async def test_authentication_rejects_bad_token():
    """Scenario 11a: authentication preservation -- a wrong/rotated token must never
    succeed and must never hang.

    Uses _FakeBridgeServer (not the real bridge.start_bridge) so the "correct"
    token lives in a plain instance attribute independent of the config
    module's shared global. This matters because bridge.py's real
    handle_connection reads its expected token via
    config.get_or_create_bridge_token() -- the SAME global bridge_client.py
    reads to build its own handshake. Patching that shared global to a "wrong"
    value would make the real bridge's expectation wrong too (since both run
    in-process and read the same config attribute), so it would appear to
    "succeed" for the wrong reason. bridge.py's own hmac.compare_digest
    rejection logic is already covered directly by
    tests/test_bridge_auth.py (re-verified unaffected by this change); this
    test's job is only to confirm bridge_client.py's new persistent-connection
    code reacts correctly (never succeeds, never hangs) to that rejection.
    """
    print("Running test_authentication_rejects_bad_token...")

    correct_token = "the-real-correct-token"
    port = ALT_PORT + 1
    server = _FakeBridgeServer(correct_token, port)
    await server.start()

    try:
        async with _fast_retry_config(max_attempts=2, base_delay=0.02, max_delay=0.05), _isolated_connection():
            with patch.object(config, "BRIDGE_CLIENT_URI", f"ws://localhost:{port}"), \
                 patch.object(config, "BRIDGE_AUTH_TOKEN", "definitely-the-wrong-token"):
                started = time.monotonic()
                raised = None
                try:
                    await bridge_client.send_figma_command("create_rectangle", {})
                except (ConnectionError, TimeoutError) as exc:
                    raised = exc
                elapsed = time.monotonic() - started
    finally:
        await server.stop()

    assert raised is not None, "Expected a bad token to raise, not silently succeed."
    assert isinstance(raised, (ConnectionError, TimeoutError)), f"Unexpected exception type: {raised!r}"
    assert elapsed < 5.0, f"Expected bad-token rejection to resolve quickly, took {elapsed:.2f}s."

    print(f"  confirmed: a wrong bridge token never succeeds and resolves within "
          f"{elapsed:.2f}s via a clear {type(raised).__name__}.")
    print("test_authentication_rejects_bad_token: PASSED\n")


async def test_authentication_preserves_valid_token_round_trip():
    """Scenario 11b: authentication preservation -- a valid token still round-trips
    successfully end to end through the real bridge with the new persistent client."""
    print("Running test_authentication_preserves_valid_token_round_trip...")

    token = config.get_or_create_bridge_token()
    server_task = asyncio.ensure_future(bridge.start_bridge(host="localhost", port=ALT_PORT + 2))
    await asyncio.sleep(0.2)

    ready_event = asyncio.Event()
    stop_event = asyncio.Event()
    plugin_task = asyncio.ensure_future(_fake_plugin_session(token, ALT_PORT + 2, ready_event, stop_event))
    await asyncio.wait_for(ready_event.wait(), timeout=3.0)

    try:
        async with _isolated_connection():
            with patch.object(config, "BRIDGE_CLIENT_URI", f"ws://localhost:{ALT_PORT + 2}"):
                result = await bridge_client.send_figma_command("create_rectangle", {"x": 0})
        assert result.get("status") == "ok", f"Expected a successful round trip, got {result}"
        assert result.get("node_id") == "e2e-node"
    finally:
        stop_event.set()
        plugin_task.cancel()
        server_task.cancel()
        for t in (plugin_task, server_task):
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    print("  confirmed: a valid bridge token still authenticates and completes a full "
          "controller -> bridge -> plugin -> bridge -> controller round trip, "
          "preserving bugfix.md 3.1/3.2/3.5/3.6 with the new persistent client.")
    print("test_authentication_preserves_valid_token_round_trip: PASSED\n")


async def test_existing_plan_execution_preserved():
    """Scenario 12: existing plan execution preservation -- login/dashboard templates
    still build their full node tree through the new persistent bridge_client,
    with the exact same succeeded/failed/total_nodes shape as before this change."""
    print("Running test_existing_plan_execution_preserved...")

    token = config.get_or_create_bridge_token()
    server_task = asyncio.ensure_future(bridge.start_bridge(host="localhost", port=ALT_PORT + 3))
    await asyncio.sleep(0.2)

    ready_event = asyncio.Event()
    stop_event = asyncio.Event()
    plugin_task = asyncio.ensure_future(_fake_plugin_session(token, ALT_PORT + 3, ready_event, stop_event))
    await asyncio.wait_for(ready_event.wait(), timeout=3.0)

    try:
        async with _isolated_connection():
            with patch.object(config, "BRIDGE_CLIENT_URI", f"ws://localhost:{ALT_PORT + 3}"):
                for name, plan, expected_nodes in [
                    ("login_template", planner._login_template(), 8),
                    ("dashboard_template", planner._dashboard_template(), 6),
                ]:
                    result = await execute_plan(plan)
                    assert result.get("status") != "error", f"{name}: unexpected error shape: {result}"
                    assert result["total_nodes"] == expected_nodes, (
                        f"{name}: expected {expected_nodes} total_nodes, got {result['total_nodes']}"
                    )
                    assert result["failed"] == 0, f"{name}: expected 0 failures, got {result}"
                    assert result["succeeded"] == result["total_nodes"], f"{name}: succeeded != total_nodes"
                    print(f"  confirmed: {name} builds the full node tree with 0 failures "
                          f"({result['succeeded']}/{result['total_nodes']} succeeded) via the "
                          f"new persistent bridge_client.")
    finally:
        stop_event.set()
        plugin_task.cancel()
        server_task.cancel()
        for t in (plugin_task, server_task):
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    print("test_existing_plan_execution_preserved: PASSED\n")


async def main():
    await test_initial_connection_and_successful_command()
    await test_connection_is_reused_across_calls()
    await test_bridge_disconnect_detected()
    await test_reconnect_after_bridge_restart()
    await test_bounded_retry_terminates()
    await test_concurrent_requests_no_cross_talk()
    await test_request_id_isolation_out_of_order_responses()
    await test_pending_request_cleanup_on_every_exit_path()
    await test_graceful_shutdown()
    await test_plugin_unavailable_gives_clear_fast_error()
    await test_authentication_rejects_bad_token()
    await test_authentication_preserves_valid_token_round_trip()
    await test_existing_plan_execution_preserved()
    print("All test_bridge_connection_reliability checks completed.")


if __name__ == "__main__":
    asyncio.run(main())
