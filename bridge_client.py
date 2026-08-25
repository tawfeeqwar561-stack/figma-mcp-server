"""
Python client for talking to the WebSocket bridge from the MCP server.

Maintains a single persistent, multiplexed connection to the bridge instead
of opening a new socket per command. Handles the controller role/token
handshake once per physical connection, correlates concurrent in-flight
requests by request_id (via a background reader task dispatching to a table
of pending futures), and reconnects with bounded exponential backoff when the
connection drops or was never established.

Public API is unchanged from the original single-shot implementation:

    await send_figma_command(action, payload) -> dict

Additive, for graceful shutdown / diagnostics:

    await close()
    is_connected() -> bool

Retry policy (deliberately asymmetric):
  - Failures BEFORE a command is sent (connect refused, handshake send
    failure) are safe to retry -- nothing has reached the plugin yet -- so
    they are retried with bounded exponential backoff.
  - Failures AFTER a command has been sent (response timeout, or the
    connection dropping while a response is still pending) are NOT retried
    automatically here, because the command may have already been executed
    on the Figma canvas; blindly resending risks creating duplicate nodes.
    These surface as the same TimeoutError/ConnectionError the original
    implementation raised, with the same messages, so callers
    (plan_executor.py's _send_command_safe, tools.py) do not need to change.
"""

import asyncio
import enum
import json
import logging
import random
import uuid
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

import config

logger = logging.getLogger(__name__)

# Re-exported for backward compatibility with any code/tests that referenced
# these as module-level constants on bridge_client directly. Source of truth
# is now config.py.
BRIDGE_URI = config.BRIDGE_CLIENT_URI
RESPONSE_TIMEOUT_SECONDS = config.BRIDGE_RESPONSE_TIMEOUT_SECONDS


class _ConnectionState(enum.Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    CLOSED = "closed"  # terminal -- close() was called, this instance is done


class _BridgeConnection:
    """
    Owns a single persistent WebSocket connection to the bridge, safe to
    share across concurrently-running asyncio tasks (multiple MCP tool
    calls can be in flight at once -- the MCP server does not serialize
    tool calls).
    """

    def __init__(self) -> None:
        self._ws: websockets.ClientConnection | None = None
        self._state = _ConnectionState.DISCONNECTED
        self._connect_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None

    def is_connected(self) -> bool:
        return self._state == _ConnectionState.CONNECTED and self._ws is not None

    async def _ensure_connected(self) -> None:
        """
        Establish the connection if not already connected, with bounded
        exponential-backoff retry across config.BRIDGE_CONNECT_MAX_ATTEMPTS
        attempts. Serialized by _connect_lock so concurrent callers don't
        race to open duplicate sockets -- the first caller connects, the
        rest observe the already-connected state once the lock releases.
        """
        if self._state == _ConnectionState.CLOSED:
            raise ConnectionError("Bridge client has been shut down.")

        async with self._connect_lock:
            if self.is_connected():
                return  # another task connected while we waited for the lock

            self._state = _ConnectionState.CONNECTING
            last_exc: Exception | None = None

            for attempt in range(1, config.BRIDGE_CONNECT_MAX_ATTEMPTS + 1):
                try:
                    ws = await websockets.connect(
                        config.BRIDGE_CLIENT_URI,
                        ping_interval=config.BRIDGE_PING_INTERVAL_SECONDS,
                        ping_timeout=config.BRIDGE_PING_TIMEOUT_SECONDS,
                    )
                    token = config.get_or_create_bridge_token()
                    await ws.send(json.dumps({"role": "controller", "token": token}))

                    self._ws = ws
                    self._state = _ConnectionState.CONNECTED
                    self._reader_task = asyncio.ensure_future(self._reader_loop(ws))
                    logger.info("Connected to bridge at %s (attempt %d).", config.BRIDGE_CLIENT_URI, attempt)
                    return
                except (OSError, WebSocketException) as exc:
                    last_exc = exc
                    logger.warning(
                        "Bridge connect attempt %d/%d failed: %s",
                        attempt, config.BRIDGE_CONNECT_MAX_ATTEMPTS, exc,
                    )
                    if attempt < config.BRIDGE_CONNECT_MAX_ATTEMPTS:
                        await asyncio.sleep(_backoff_delay(attempt))

            self._state = _ConnectionState.DISCONNECTED
            raise ConnectionError(
                "Could not reach the bridge server. Is bridge.py running?"
            ) from last_exc

    async def _reader_loop(self, ws) -> None:
        """
        Background task, one per physical connection: reads every message
        off the socket and dispatches it to whichever pending future is
        waiting on its request_id. This is what makes concurrent in-flight
        requests over one shared connection safe -- each caller's
        send_figma_command awaits its own future, not "the next message on
        the socket".
        """
        try:
            async for raw_message in ws:
                try:
                    data = json.loads(raw_message)
                except json.JSONDecodeError:
                    logger.warning("Received non-JSON message from bridge, dropping: %s", raw_message)
                    continue

                request_id = data.get("request_id") if isinstance(data, dict) else None
                if request_id and request_id in self._pending:
                    future = self._pending[request_id]
                    if not future.done():
                        future.set_result(data)
                    continue

                if request_id:
                    # A late/expired response (its own caller already gave up
                    # and moved on) -- mirrors bridge.py's own handling of an
                    # unmatched request_id. Not an error, just a drop.
                    logger.warning("No pending request for request_id=%s, dropping.", request_id)
                    continue

                if isinstance(data, dict) and data.get("status") == "error":
                    # No request_id on an error response only happens for the
                    # bridge's own handshake rejection (bad/rotated token) --
                    # it has nothing else to correlate it to. Fail every
                    # request currently waiting on this connection with the
                    # bridge's own message, since none of them will ever get
                    # a real answer on a connection that was never accepted.
                    logger.error("Bridge rejected this connection: %s", data.get("message"))
                    self._fail_all_pending(ConnectionError(str(data.get("message") or "Bridge rejected the connection.")))
        except ConnectionClosed:
            logger.info("Bridge connection closed.")
        finally:
            if self._ws is ws:
                self._ws = None
                if self._state != _ConnectionState.CLOSED:
                    self._state = _ConnectionState.DISCONNECTED
            self._fail_all_pending(ConnectionError("Could not reach the bridge server. Is bridge.py running?"))

    def _fail_all_pending(self, exc: Exception) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(exc)

    async def send_figma_command(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        request_id = str(uuid.uuid4())
        message = {"request_id": request_id, "action": action, "payload": payload}

        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future

        try:
            await self._send_with_retry(message)
            logger.info("Sent command: %s", message)

            try:
                response = await asyncio.wait_for(future, timeout=config.BRIDGE_RESPONSE_TIMEOUT_SECONDS)
            except asyncio.TimeoutError as exc:
                # Deliberately NOT retried: the command may already have
                # reached the plugin and be executing -- resending risks a
                # duplicate node. Same exception type/message as before.
                logger.error("Timed out waiting for plugin response to %s", action)
                raise TimeoutError(
                    f"No response from Figma plugin for action '{action}' "
                    f"within {config.BRIDGE_RESPONSE_TIMEOUT_SECONDS}s. Is the plugin running?"
                ) from exc

            logger.info("Received response: %s", response)
            return response
        finally:
            # Always remove this request's entry, on every exit path
            # (success, timeout, or connection error) -- prevents the
            # pending-futures table from growing without bound.
            self._pending.pop(request_id, None)

    async def _send_with_retry(self, message: dict[str, Any]) -> None:
        """
        Ensure connected and send `message`, retrying only failures that
        happen before the message is confirmed sent (connect failure, or the
        socket dying on this specific send attempt). Bounded by
        config.BRIDGE_CONNECT_MAX_ATTEMPTS total attempts; never loops
        forever.
        """
        last_exc: Exception | None = None
        for attempt in range(1, config.BRIDGE_CONNECT_MAX_ATTEMPTS + 1):
            try:
                await self._ensure_connected()
                async with self._send_lock:
                    # Guard against the connection having been torn down
                    # between _ensure_connected() returning and acquiring
                    # the send lock (e.g. a concurrent disconnect).
                    if not self.is_connected():
                        raise ConnectionClosed(None, None)
                    await self._ws.send(json.dumps(message))
                return
            except ConnectionError:
                # _ensure_connected already retried internally and gave up --
                # don't multiply attempts on top of that, just propagate.
                raise
            except (ConnectionClosed, OSError) as exc:
                last_exc = exc
                logger.warning(
                    "Send attempt %d/%d failed: %s", attempt, config.BRIDGE_CONNECT_MAX_ATTEMPTS, exc
                )
                if attempt < config.BRIDGE_CONNECT_MAX_ATTEMPTS:
                    await asyncio.sleep(_backoff_delay(attempt))

        logger.error("Could not send command after %d attempts: %s", config.BRIDGE_CONNECT_MAX_ATTEMPTS, last_exc)
        raise ConnectionError(
            "Could not reach the bridge server. Is bridge.py running?"
        ) from last_exc

    async def close(self) -> None:
        """Graceful shutdown: stop retrying/reconnecting, fail anything still pending, close the socket."""
        self._state = _ConnectionState.CLOSED
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reader_task = None

        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass

        self._fail_all_pending(ConnectionError("Bridge client is shutting down."))
        self._pending.clear()


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff from config.BRIDGE_RECONNECT_BASE_DELAY_SECONDS, capped
    at config.BRIDGE_RECONNECT_MAX_DELAY_SECONDS, with up to 20% jitter to avoid
    multiple reconnecting clients retrying in lockstep."""
    base = config.BRIDGE_RECONNECT_BASE_DELAY_SECONDS * (2 ** (attempt - 1))
    capped = min(base, config.BRIDGE_RECONNECT_MAX_DELAY_SECONDS)
    jitter = capped * random.uniform(0, 0.2)
    return capped + jitter


# Module-level singleton -- one persistent connection shared by every caller
# in this process (tools.py, plan_executor.py). Concurrency safety is
# handled inside _BridgeConnection itself (locks + per-request futures), so
# sharing this instance across concurrently-running MCP tool calls is safe.
_connection = _BridgeConnection()


async def send_figma_command(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    """
    Send a single command to the Figma plugin via the bridge and wait
    for its result. Reuses a persistent connection across calls; reconnects
    with bounded backoff if needed. Signature and exception contract
    (TimeoutError, ConnectionError, same messages) are unchanged from the
    original single-shot-connection implementation.
    """
    return await _connection.send_figma_command(action, payload)


async def close() -> None:
    """Gracefully shut down the shared bridge connection (cancel the reader
    task, fail any still-pending requests, close the socket). Safe to call
    even if never connected."""
    await _connection.close()


def is_connected() -> bool:
    """Whether the shared bridge connection currently believes it's connected."""
    return _connection.is_connected()
