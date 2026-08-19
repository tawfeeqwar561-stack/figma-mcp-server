"""
WebSocket bridge server.
Routes commands from controllers (MCP server) to plugins (Figma),
and routes plugin results back to the specific controller that requested them.
"""

import asyncio
import hmac
import json
import logging
import time
from typing import Any

import websockets
from websockets.asyncio.server import ServerConnection
from websockets.datastructures import Headers
from websockets.http11 import Response

import config

logger = logging.getLogger(__name__)

_plugins: set[ServerConnection] = set()
_controllers: set[ServerConnection] = set()

# Maps a request_id -> (the controller websocket that should receive the
# result, the time.monotonic() timestamp it was registered at). The
# timestamp lets _sweep_pending_requests evict entries that never get a
# plugin response, bounding memory growth (see H-3).
_pending_requests: dict[str, tuple[ServerConnection, float]] = {}

# Chosen to sit above bridge_client.py's own RESPONSE_TIMEOUT_SECONDS (10s),
# so the bridge does not evict an entry the controller is still legitimately
# waiting on.
_PENDING_REQUEST_TTL_SECONDS = 20.0
_PENDING_REQUEST_SWEEP_INTERVAL_SECONDS = 5.0


async def handle_connection(websocket: ServerConnection) -> None:
    try:
        first_message = await websocket.recv()
        data = json.loads(first_message)
        role = data.get("role")
        token = data.get("token")
    except Exception:
        logger.warning("Client failed to identify role, closing connection.")
        await websocket.close()
        return

    expected_token = config.get_or_create_bridge_token()
    if not isinstance(token, str) or not hmac.compare_digest(token, expected_token):
        logger.warning("Client presented an invalid or missing bridge token, closing connection.")
        await websocket.send(json.dumps({"status": "error", "message": "Invalid or missing bridge token"}))
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
            try:
                data = json.loads(raw_message)
            except json.JSONDecodeError:
                logger.warning("Controller sent non-JSON message, rejecting: %s", raw_message)
                await websocket.send(json.dumps({
                    "request_id": None,
                    "status": "error",
                    "message": "Message is not valid JSON",
                }))
                continue

            valid_shape = (
                isinstance(data, dict)
                and isinstance(data.get("action"), str)
                and isinstance(data.get("payload", {}), dict)
            )
            if not valid_shape:
                logger.warning("Controller sent malformed command shape, rejecting: %s", raw_message)
                await websocket.send(json.dumps({
                    "request_id": data.get("request_id") if isinstance(data, dict) else None,
                    "status": "error",
                    "message": "Malformed command: 'action' must be a string and 'payload' must be a dict",
                }))
                continue

            request_id = data.get("request_id")
            if request_id:
                _pending_requests[request_id] = (websocket, time.monotonic())
            logger.info("Command from controller: %s", raw_message)
            await _relay_to_plugins(raw_message)
    except websockets.exceptions.ConnectionClosed:
        logger.info("Controller disconnected: %s", websocket.remote_address)
    finally:
        _controllers.discard(websocket)
        # A disconnected controller can never receive a result, so prune
        # its entries immediately rather than waiting for TTL sweep.
        stale_request_ids = [
            request_id
            for request_id, (controller_ws, _timestamp) in _pending_requests.items()
            if controller_ws is websocket
        ]
        for request_id in stale_request_ids:
            _pending_requests.pop(request_id, None)


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

    controller_ws, _timestamp = _pending_requests.pop(request_id, (None, None)) if request_id else (None, None)

    if controller_ws is None:
        logger.warning("No matching controller for request_id=%s", request_id)
        return

    try:
        await controller_ws.send(raw_message)
        logger.info("Result routed to controller successfully.")
    except websockets.exceptions.ConnectionClosed:
        logger.warning("Controller connection already closed, couldn't deliver result for request_id=%s", request_id)


# Hosts start_bridge may bind to without an explicitly configured
# BRIDGE_AUTH_TOKEN (see the fail-closed check added for H-7). Also used to
# decide whether the plain-HTTP GET /token convenience endpoint (used by
# figma-plugin/ui.html, which cannot read .env or the filesystem) is
# exposed at all -- it never is on a non-loopback bind.
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


async def _process_request(connection: ServerConnection, request):
    """
    Serve a plain HTTP GET /token with the current bridge token, so the
    Figma plugin iframe (which cannot read .env or the filesystem) can
    fetch it before opening its WebSocket connection. Every other request
    (including the WebSocket upgrade handshake itself) is passed through
    unchanged by returning None. Only registered for loopback binds.
    """
    if request.path == "/token":
        token = config.get_or_create_bridge_token()
        body = json.dumps({"token": token}).encode("utf-8")
        return Response(
            200,
            "OK",
            Headers([("Content-Type", "application/json")]),
            body,
        )
    return None


async def _sweep_pending_requests() -> None:
    """
    Background task: periodically evict _pending_requests entries whose
    plugin response never arrived within _PENDING_REQUEST_TTL_SECONDS,
    bounding memory growth (H-3). Runs for the lifetime of the bridge
    process; start_bridge cancels it when the serve loop exits.
    """
    while True:
        await asyncio.sleep(_PENDING_REQUEST_SWEEP_INTERVAL_SECONDS)
        now = time.monotonic()
        stale_request_ids = [
            request_id
            for request_id, (_controller_ws, timestamp) in _pending_requests.items()
            if now - timestamp > _PENDING_REQUEST_TTL_SECONDS
        ]
        for request_id in stale_request_ids:
            _pending_requests.pop(request_id, None)
        if stale_request_ids:
            logger.info("Swept %d stale pending request(s).", len(stale_request_ids))


async def start_bridge(host: str = "localhost", port: int = 8765) -> None:
    if host.lower() not in _LOOPBACK_HOSTS and not config.BRIDGE_AUTH_TOKEN:
        raise RuntimeError(
            f"Refusing to bind bridge to non-loopback host '{host}' without an explicitly "
            "configured BRIDGE_AUTH_TOKEN. Set BRIDGE_AUTH_TOKEN in .env before exposing the "
            "bridge beyond localhost."
        )

    logger.info("Starting bridge server on ws://%s:%d", host, port)
    process_request = _process_request if host.lower() in _LOOPBACK_HOSTS else None
    sweep_task = asyncio.create_task(_sweep_pending_requests())
    try:
        async with websockets.serve(handle_connection, host, port, process_request=process_request):
            await asyncio.Future()
    finally:
        sweep_task.cancel()
        try:
            await sweep_task
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(start_bridge())