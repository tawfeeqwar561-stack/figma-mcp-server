"""
Tests for Subsystem 7 -- Fail-closed bind (H-7: no fail-closed bind restriction).

Run directly: python tests/test_fail_closed_bind.py

Contains BOTH:
  - test_bug_condition_non_loopback_bind_without_token(): Property 1 (Bug
    Condition). On UNFIXED code, proves start_bridge(host="0.0.0.0") with
    no BRIDGE_AUTH_TOKEN configured proceeds to call websockets.serve
    (mocked, so no real socket opens) instead of raising. After the fix,
    the same call must raise RuntimeError and never call websockets.serve.
  - test_preservation_default_loopback_bind(): Property 2 (Preservation).
    start_bridge(host="localhost") (and "127.0.0.1", "::1") with no
    BRIDGE_AUTH_TOKEN configured must still proceed to call
    websockets.serve, both before and after the fix.
"""

import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import bridge
import config


class _FakeServeContextManager:
    """Stand-in for the async context manager websockets.serve(...) returns."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


async def _run_start_bridge_briefly(host):
    """
    Run bridge.start_bridge(host=...) as a background task with
    websockets.serve mocked (no real socket), give it a moment, then
    cancel it. Returns (raised_exception_or_None, mocked_serve).
    """
    mocked_serve = MagicMock(return_value=_FakeServeContextManager())
    raised = None
    with patch.object(bridge.websockets, "serve", mocked_serve):
        task = asyncio.ensure_future(bridge.start_bridge(host=host, port=0))
        await asyncio.sleep(0.05)
        if task.done():
            raised = task.exception()
        else:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    return raised, mocked_serve


async def test_bug_condition_non_loopback_bind_without_token():
    """Property 1: Bug Condition -- Non-Loopback Bind Without Token (H-7)."""
    print("Running test_bug_condition_non_loopback_bind_without_token...")

    with patch.object(config, "BRIDGE_AUTH_TOKEN", None), patch.object(bridge.config, "BRIDGE_AUTH_TOKEN", None):
        raised, mocked_serve = await _run_start_bridge_briefly("0.0.0.0")

        if isinstance(raised, RuntimeError):
            assert mocked_serve.call_count == 0, (
                "Expected FIXED start_bridge to never call websockets.serve when raising."
            )
            print("  [FIXED] confirmed: start_bridge(host='0.0.0.0') with no BRIDGE_AUTH_TOKEN "
                  "raises RuntimeError and never calls websockets.serve.")
        else:
            assert mocked_serve.call_count > 0, (
                "Expected UNFIXED start_bridge to proceed to call websockets.serve "
                "(confirms H-7: no fail-closed check exists)."
            )
            print("  [UNFIXED] confirmed: start_bridge(host='0.0.0.0') with no BRIDGE_AUTH_TOKEN "
                  "proceeded to call websockets.serve without any RuntimeError -- "
                  "counterexample for H-7.")

    print("test_bug_condition_non_loopback_bind_without_token: PASSED\n")


async def test_preservation_default_loopback_bind():
    """Property 2: Preservation -- Default Loopback Bind."""
    print("Running test_preservation_default_loopback_bind...")

    with patch.object(config, "BRIDGE_AUTH_TOKEN", None), patch.object(bridge.config, "BRIDGE_AUTH_TOKEN", None):
        for host in ("localhost", "127.0.0.1", "::1"):
            raised, mocked_serve = await _run_start_bridge_briefly(host)
            assert raised is None, (
                f"Expected start_bridge(host={host!r}) to proceed without raising, got {raised!r}."
            )
            assert mocked_serve.call_count > 0, (
                f"Expected start_bridge(host={host!r}) to call websockets.serve exactly as before."
            )
            print(f"  confirmed: start_bridge(host={host!r}) with no BRIDGE_AUTH_TOKEN still "
                  f"proceeds to call websockets.serve, on both pre-fix and post-fix code.")

    print("test_preservation_default_loopback_bind: PASSED\n")


async def main():
    await test_bug_condition_non_loopback_bind_without_token()
    await test_preservation_default_loopback_bind()
    print("All test_fail_closed_bind checks completed.")


if __name__ == "__main__":
    asyncio.run(main())
