"""
Python client for talking to the WebSocket bridge from the MCP server.
Handles connecting as a "controller" and correlating requests with responses.
"""

import asyncio
import json
import logging
import uuid
from typing import Any

import websockets

logger = logging.getLogger(__name__)

BRIDGE_URI = "ws://localhost:8765"
RESPONSE_TIMEOUT_SECONDS = 10.0


async def send_figma_command(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    """
    Send a single command to the Figma plugin via the bridge and wait
    for its result.

    Args:
        action: Command name (must match a handler in code.js).
        payload: Command-specific arguments.

    Returns:
        The plugin's result dict, e.g. {"status": "ok", "node_id": "..."}.

    Raises:
        TimeoutError: If no response arrives within RESPONSE_TIMEOUT_SECONDS.
        ConnectionError: If the bridge itself can't be reached.
    """
    request_id = str(uuid.uuid4())
    message = {"request_id": request_id, "action": action, "payload": payload}

    try:
        async with websockets.connect(BRIDGE_URI) as websocket:
            await websocket.send(json.dumps({"role": "controller"}))
            await websocket.send(json.dumps(message))
            logger.info("Sent command: %s", message)

            raw_response = await asyncio.wait_for(
                websocket.recv(), timeout=RESPONSE_TIMEOUT_SECONDS
            )
            response = json.loads(raw_response)
            logger.info("Received response: %s", response)
            return response

    except asyncio.TimeoutError as exc:
        logger.error("Timed out waiting for plugin response to %s", action)
        raise TimeoutError(
            f"No response from Figma plugin for action '{action}' "
            f"within {RESPONSE_TIMEOUT_SECONDS}s. Is the plugin running?"
        ) from exc
    except (websockets.exceptions.ConnectionClosed, OSError) as exc:
        logger.error("Could not connect to bridge: %s", exc)
        raise ConnectionError(
            "Could not reach the bridge server. Is bridge.py running?"
        ) from exc