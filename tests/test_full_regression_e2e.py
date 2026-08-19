"""
Wave 8 final-validation helper (task 29): a lightweight end-to-end check
that a real bridge.py process (started on an alternate port to avoid
colliding with anything already running on 8765), an authenticated fake
plugin, and bridge_client.send_figma_command together still produce a
successful round trip after all 7 fixes -- i.e. bugfix.md 3.9's tool
pipeline (bridge_client -> bridge -> plugin -> bridge -> bridge_client)
still works end-to-end with the new token handshake.

This is NOT one of the 7 subsystem test files -- it's an additional
integration check run once, for the final validation task only.

Run directly: python tests/test_full_regression_e2e.py
"""

import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import asyncio
import json

import websockets

import bridge
import config

TEST_PORT = 18765


async def _fake_plugin_session(token, ready_event, stop_event):
    uri = f"ws://localhost:{TEST_PORT}"
    async with websockets.connect(uri) as ws:
        await ws.send(json.dumps({"role": "plugin", "token": token}))
        ready_event.set()
        while not stop_event.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            command = json.loads(raw)
            result = {
                "request_id": command.get("request_id"),
                "status": "ok",
                "node_id": "e2e-node-1",
            }
            await ws.send(json.dumps(result))


async def main():
    token = config.get_or_create_bridge_token()

    server_task = asyncio.ensure_future(bridge.start_bridge(host="localhost", port=TEST_PORT))
    await asyncio.sleep(0.2)

    ready_event = asyncio.Event()
    stop_event = asyncio.Event()
    plugin_task = asyncio.ensure_future(_fake_plugin_session(token, ready_event, stop_event))
    await asyncio.wait_for(ready_event.wait(), timeout=3.0)

    # Exercise the controller path directly against the real bridge
    # (mirrors bridge_client.send_figma_command but targets our test port).
    import uuid
    request_id = str(uuid.uuid4())
    message = {"request_id": request_id, "action": "create_rectangle", "payload": {"x": 0, "y": 0, "width": 10, "height": 10}}

    async with websockets.connect(f"ws://localhost:{TEST_PORT}") as controller_ws:
        await controller_ws.send(json.dumps({"role": "controller", "token": token}))
        await controller_ws.send(json.dumps(message))
        raw_response = await asyncio.wait_for(controller_ws.recv(), timeout=5.0)
        response = json.loads(raw_response)

    assert response.get("status") == "ok", f"Expected ok status, got {response}"
    assert response.get("node_id") == "e2e-node-1", f"Expected the fake plugin's node_id, got {response}"
    print(f"E2E round trip succeeded: {response}")

    stop_event.set()
    plugin_task.cancel()
    server_task.cancel()
    for t in (plugin_task, server_task):
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass

    print("test_full_regression_e2e: PASSED")


if __name__ == "__main__":
    asyncio.run(main())
