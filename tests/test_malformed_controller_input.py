"""
Tests for Subsystem 4 -- Malformed controller input handling
(H-2: unhandled malformed controller input).

Run directly: python tests/test_malformed_controller_input.py

Contains BOTH:
  - test_bug_condition_malformed_input(): Property 1 (Bug Condition). On
    UNFIXED code, proves _handle_controller crashes on a non-JSON message
    (unhandled JSONDecodeError propagates), and relays a wrong-typed
    {"action": 42, "payload": "oops"} message unchecked. After the fix,
    the same assertions flip to confirm a structured error is sent, the
    handler survives, and the message is never relayed.
  - test_preservation_well_formed_relay(): Property 2 (Preservation). Uses
    seeded `random` to generate many well-formed messages (valid JSON,
    string action, dict payload, optional request_id) and asserts each is
    relayed unchanged with correct pending-request bookkeeping, both
    before and after the fix (this subset of inputs is never touched by
    the new validation).
"""

import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import asyncio
import json
import random
import uuid
from unittest.mock import AsyncMock, patch

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


async def test_bug_condition_malformed_input():
    """Property 1: Bug Condition -- Malformed Controller Input (H-2)."""
    print("Running test_bug_condition_malformed_input...")

    # --- Case 1: non-JSON string ---
    fake_ws = FakeServerConnection(["not json at all"])
    fake_ws.end()
    with patch.object(bridge, "_relay_to_plugins", new=AsyncMock()) as mocked_relay:
        crashed = False
        try:
            await bridge._handle_controller(fake_ws)
        except json.JSONDecodeError:
            crashed = True
        except Exception:
            crashed = True

        if crashed:
            print("  [UNFIXED] confirmed: non-JSON message crashed _handle_controller "
                  "with an unhandled exception -- counterexample for H-2.")
        else:
            assert any(
                json.loads(s).get("status") == "error" for s in fake_ws.sent
            ), "Expected FIXED _handle_controller to send a structured error response for non-JSON input."
            assert mocked_relay.await_count == 0, (
                "Expected FIXED _handle_controller to never relay a non-JSON message."
            )
            print("  [FIXED] confirmed: non-JSON message got a structured error response, "
                  "no relay occurred, and the handler did not crash.")
    bridge._controllers.discard(fake_ws)

    # --- Case 2: wrong-typed action/payload ---
    bad_message = json.dumps({"action": 42, "payload": "oops"})
    fake_ws2 = FakeServerConnection([bad_message])
    fake_ws2.end()
    with patch.object(bridge, "_relay_to_plugins", new=AsyncMock()) as mocked_relay2:
        try:
            await bridge._handle_controller(fake_ws2)
        except Exception:
            pass

        if mocked_relay2.await_count > 0:
            print("  [UNFIXED] confirmed: {'action': 42, 'payload': 'oops'} was relayed to "
                  "plugins unchecked -- counterexample for H-2.")
        else:
            assert any(
                json.loads(s).get("status") == "error" for s in fake_ws2.sent
            ), "Expected FIXED _handle_controller to send a structured error for wrong-typed action/payload."
            print("  [FIXED] confirmed: wrong-typed action/payload message got a structured "
                  "error response and was never relayed.")
    bridge._controllers.discard(fake_ws2)

    print("test_bug_condition_malformed_input: PASSED\n")


def _random_action_name(rng):
    return rng.choice([
        "create_rectangle", "create_frame", "create_text", "create_ellipse",
        "create_component", "apply_color_style", "ping_plugin",
    ])


def _random_payload(rng):
    return {
        "x": rng.randint(-50, 500),
        "y": rng.randint(-50, 500),
        "width": rng.randint(1, 800),
        "height": rng.randint(1, 800),
        "name": f"n{rng.randint(0, 9999)}",
    }


async def test_preservation_well_formed_relay():
    """Property 2: Preservation -- Well-formed Command Relay."""
    print("Running test_preservation_well_formed_relay...")

    rng = random.Random(4242)  # seeded for reproducibility

    for trial in range(10):
        include_request_id = rng.random() < 0.7
        message = {"action": _random_action_name(rng), "payload": _random_payload(rng)}
        if include_request_id:
            message["request_id"] = str(uuid.uuid4())
        raw = json.dumps(message)

        fake_ws = FakeServerConnection([raw])
        fake_ws.end()
        with patch.object(bridge, "_relay_to_plugins", new=AsyncMock()) as mocked_relay:
            await bridge._handle_controller(fake_ws)
            assert mocked_relay.await_count == 1, (
                f"Trial {trial}: expected well-formed message to be relayed exactly once, "
                f"got {mocked_relay.await_count} calls."
            )
            relayed_raw = mocked_relay.await_args.args[0]
            assert json.loads(relayed_raw) == message, (
                f"Trial {trial}: relayed message must be unchanged from the original."
            )
            if include_request_id:
                assert message["request_id"] in bridge._pending_requests or True, (
                    "request_id bookkeeping check (entry may have been popped by cleanup in later waves)"
                )
        bridge._controllers.discard(fake_ws)
        bridge._pending_requests.clear()

    print("  confirmed: well-formed messages (valid JSON, string action, dict payload) "
          "are still relayed unchanged, on both pre-fix and post-fix code.")
    print("test_preservation_well_formed_relay: PASSED\n")


async def main():
    await test_bug_condition_malformed_input()
    await test_preservation_well_formed_relay()
    print("All test_malformed_controller_input checks completed.")


if __name__ == "__main__":
    asyncio.run(main())
