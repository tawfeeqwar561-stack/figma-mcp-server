"""
Manual live verification: sends one real command through the actual
controller code path (bridge_client.send_figma_command) to whatever bridge
+ Figma plugin are currently running, and asserts on the result.

This is intentionally different from the automated regression tests
(test_full_regression_e2e.py, test_bridge_connection_reliability.py), which
spin up their own bridge + a fake plugin so they're self-contained and don't
require Figma to be open. This script is the deliberately low-tech "does my
actual live local setup work" check described in README.md's setup steps --
run it after starting `python bridge.py` and opening the Figma plugin by
hand, without needing Ollama or the MCP Inspector.

Requires:
  - bridge.py running locally (python bridge.py)
  - the Figma plugin open and connected (for the "ok" case below; if it's
    not connected, the bridge's own plugin-unavailable error is treated as
    an expected, clearly-reported outcome, not a crash)

Run directly: python tests/test_send_command.py
Exits non-zero on any unexpected failure (bridge unreachable, bad auth,
malformed/unexpected response shape).
"""

import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import asyncio
import logging

import bridge_client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def main() -> None:
    command = {
        "x": 100, "y": 100, "width": 200, "height": 150,
        "color": {"r": 0.2, "g": 0.6, "b": 1.0},
    }

    try:
        try:
            # The real production controller path -- authenticates with
            # the current bridge token automatically and reuses the
            # persistent connection, exactly as tools.create_rectangle does.
            result = await bridge_client.send_figma_command("create_rectangle", command)
        except ConnectionError as exc:
            print(f"FAILED: could not reach the bridge -- is 'python bridge.py' running? ({exc})")
            sys.exit(1)
        except TimeoutError as exc:
            print(f"FAILED: bridge did not respond in time ({exc})")
            sys.exit(1)

        assert isinstance(result, dict), f"Expected a dict response, got {type(result)!r}: {result!r}"
        assert "status" in result, f"Expected a 'status' key in the response, got {result!r}"

        if result.get("status") == "error" and "plugin" in result.get("message", "").lower():
            print(
                "NO PLUGIN CONNECTED: the bridge is reachable and authentication succeeded, "
                f"but no Figma plugin is currently attached ({result.get('message')}).\n"
                "Open the Figma plugin (Plugins -> Development -> MCP Bridge Plugin) and re-run "
                "this script to verify a full controller -> bridge -> plugin round trip."
            )
            sys.exit(1)

        assert result.get("status") == "ok", f"Expected status 'ok', got {result!r}"
        assert result.get("node_id"), f"Expected a node_id in a successful response, got {result!r}"

        print(f"PASSED: create_rectangle round-tripped successfully, node_id={result['node_id']!r}")
    finally:
        # Clean, graceful shutdown of the persistent connection this
        # one-shot script opened, so it doesn't leave a dangling background
        # reader task behind on exit.
        await bridge_client.close()


if __name__ == "__main__":
    asyncio.run(main())
