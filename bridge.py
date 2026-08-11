"""
WebSocket bridge server.
Routes commands from controllers (MCP server) to plugins (Figma),
and routes plugin results back to the specific controller that requested them.
"""

import asyncio
import json
import logging
from typing import Any

import websockets
from websockets.asyncio.server import ServerConnection

logger = logging.getLogger(__name__)

_plugins: set[ServerConnection] = set()
_controllers: set[ServerConnection] = set()

# Maps a request_id -> the controller websocket that should receive the result.
_pending_requests: dict[str, ServerConnection] = {}


async def handle_connection(websocket: ServerConnection) -> None:
    try:
        first_message = await websocket.recv()
        data = json.loads(first_message)
        role = data.get("role")
    except Exception:
        logger.warning("Client failed to identify role, closing connection.")
        await websocket.close()
        return

    if role == "plugin":
        await _handle_plugin(websocket)
    elif role == "controller":
        await _handle_controller(websocket)
    else:
        logger.warning("Unknown role %r, closing connection.", role)
        await websocket.close()


async def _handle_plugin(websocket: ServerConnection) -> None:
    logger.info("Plugin connected: %s", websocket.remote_address)
    _plugins.add(websocket)
    try:
        async for raw_message in websocket:
            logger.info("Result from plugin: %s", raw_message)
            await _route_result_to_controller(raw_message)
    except websockets.exceptions.ConnectionClosed:
        logger.info("Plugin disconnected: %s", websocket.remote_address)
    finally:
        _plugins.discard(websocket)


async def _handle_controller(websocket: ServerConnection) -> None:
    logger.info("Controller connected: %s", websocket.remote_address)
    _controllers.add(websocket)
    try:
        async for raw_message in websocket:
            data = json.loads(raw_message)
            request_id = data.get("request_id")
            if request_id:
                _pending_requests[request_id] = websocket
            logger.info("Command from controller: %s", raw_message)
            await _relay_to_plugins(raw_message)
    except websockets.exceptions.ConnectionClosed:
        logger.info("Controller disconnected: %s", websocket.remote_address)
    finally:
        _controllers.discard(websocket)


async def _relay_to_plugins(message: str) -> None:
    if not _plugins:
        logger.warning("No plugin connected — command dropped: %s", message)
        return
    for plugin_ws in list(_plugins):
        await plugin_ws.send(message)


async def _route_result_to_controller(raw_message: str) -> None:
    """Send a plugin's result back to whichever controller made the matching request."""
    try:
        data = json.loads(raw_message)
    except json.JSONDecodeError:
        logger.warning("Plugin sent non-JSON result, dropping: %s", raw_message)
        return

    request_id = data.get("request_id")
    logger.info("Routing result for request_id=%s, pending=%s", request_id, list(_pending_requests.keys()))

    controller_ws = _pending_requests.pop(request_id, None) if request_id else None

    if controller_ws is None:
        logger.warning("No matching controller for request_id=%s", request_id)
        return

    try:
        await controller_ws.send(raw_message)
        logger.info("Result routed to controller successfully.")
    except websockets.exceptions.ConnectionClosed:
        logger.warning("Controller connection already closed, couldn't deliver result for request_id=%s", request_id)
    request_id = data.get("request_id")
    controller_ws = _pending_requests.pop(request_id, None) if request_id else None


async def start_bridge(host: str = "localhost", port: int = 8765) -> None:
    logger.info("Starting bridge server on ws://%s:%d", host, port)
    async with websockets.serve(handle_connection, host, port):
        await asyncio.Future()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(start_bridge())