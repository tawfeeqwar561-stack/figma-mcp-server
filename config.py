"""
Centralized configuration loader.
Reads environment variables from .env and validates required values.
"""

import os
import secrets
from dotenv import load_dotenv

load_dotenv()

FIGMA_ACCESS_TOKEN: str | None = os.getenv("FIGMA_ACCESS_TOKEN")
FIGMA_FILE_ID: str | None = os.getenv("FIGMA_FILE_ID")
OLLAMA_BASE_URL: str = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")

# Explicitly configured shared secret for the bridge's WebSocket handshake.
# Left as None if the operator hasn't set it -- in that case
# get_or_create_bridge_token() falls back to an auto-generated, gitignored
# local file. start_bridge() (bridge.py) treats THIS raw env var (not the
# file fallback) as the only acceptable token source for non-loopback binds.
BRIDGE_AUTH_TOKEN: str | None = os.getenv("BRIDGE_AUTH_TOKEN")

# Overall deadline for a single execute_plan() call (plan_executor.py).
# 120s default sits generously above a realistic worst case within the
# schema caps (~10 elements -> roughly a dozen node-creation round trips
# at up to bridge_client's 10s timeout each), while still bounding
# runaway executions -- see H-6 in bridge-security-hardening.
PLAN_EXECUTION_TIMEOUT_SECONDS: float = float(os.getenv("PLAN_EXECUTION_TIMEOUT_SECONDS", "120"))

# --- Bridge client connection settings (persistent-connection reliability layer) ---
# Previously hardcoded directly in bridge_client.py; unified here so they can be
# tuned via .env without touching code, matching the PLAN_EXECUTION_TIMEOUT_SECONDS
# pattern above. Defaults reproduce the exact prior behavior (10s response wait,
# ws://localhost:8765).
BRIDGE_CLIENT_URI: str = os.getenv("BRIDGE_CLIENT_URI", "ws://localhost:8765")
BRIDGE_RESPONSE_TIMEOUT_SECONDS: float = float(os.getenv("BRIDGE_RESPONSE_TIMEOUT_SECONDS", "10.0"))

# Bounded connect/reconnect retry policy. BRIDGE_CONNECT_MAX_ATTEMPTS caps total
# attempts per connect cycle (never infinite); the delay between attempts grows
# exponentially from the base, capped at the max, plus small jitter to avoid
# thundering-herd reconnects if multiple controllers reconnect at once.
BRIDGE_CONNECT_MAX_ATTEMPTS: int = int(os.getenv("BRIDGE_CONNECT_MAX_ATTEMPTS", "3"))
BRIDGE_RECONNECT_BASE_DELAY_SECONDS: float = float(os.getenv("BRIDGE_RECONNECT_BASE_DELAY_SECONDS", "0.5"))
BRIDGE_RECONNECT_MAX_DELAY_SECONDS: float = float(os.getenv("BRIDGE_RECONNECT_MAX_DELAY_SECONDS", "4.0"))

# WebSocket ping/pong keepalive tuning, used by BOTH bridge.py's
# websockets.serve(...) and bridge_client.py's websockets.connect(...) so
# dead-peer detection (e.g. a laptop sleeping, a network drop with no
# clean close frame) happens on the same known cadence on both ends,
# instead of each side silently relying on the `websockets` library's own
# untouched built-in defaults (20s/20s) with no way to tune them here.
# Values match the library's own defaults, so omitting these env vars
# reproduces the exact previous behavior.
BRIDGE_PING_INTERVAL_SECONDS: float = float(os.getenv("BRIDGE_PING_INTERVAL_SECONDS", "20.0"))
BRIDGE_PING_TIMEOUT_SECONDS: float = float(os.getenv("BRIDGE_PING_TIMEOUT_SECONDS", "20.0"))

_BRIDGE_TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".bridge_token")


def validate_config() -> None:
    """Raise a clear error if required config is missing."""
    if not FIGMA_ACCESS_TOKEN:
        raise ValueError(
            "FIGMA_ACCESS_TOKEN is not set. Check your .env file."
        )
    if not FIGMA_FILE_ID:
        raise ValueError(
            "FIGMA_FILE_ID is not set. Check your .env file."
        )


def get_or_create_bridge_token() -> str:
    """
    Return the shared secret controllers/plugins must present when
    connecting to the bridge.

    Precedence:
    1. BRIDGE_AUTH_TOKEN env var, if set.
    2. Otherwise, read (or create) a local `.bridge_token` file next to
       this module, generating a new `secrets.token_urlsafe(32)` value
       the first time. Best-effort `os.chmod(..., 0o600)` is applied to
       restrict access on POSIX hosts; on Windows this does not enforce
       the same ACL semantics, so this fallback is intended for the
       trusted-local-user / loopback-only use case (see bridge.py's
       fail-closed non-loopback bind check).
    """
    if BRIDGE_AUTH_TOKEN:
        return BRIDGE_AUTH_TOKEN

    if os.path.exists(_BRIDGE_TOKEN_FILE):
        with open(_BRIDGE_TOKEN_FILE, "r", encoding="utf-8") as f:
            existing = f.read().strip()
        if existing:
            return existing

    token = secrets.token_urlsafe(32)
    with open(_BRIDGE_TOKEN_FILE, "w", encoding="utf-8") as f:
        f.write(token)
    try:
        os.chmod(_BRIDGE_TOKEN_FILE, 0o600)
    except OSError:
        pass  # best-effort on platforms without POSIX-style permissions (e.g. Windows)
    return token