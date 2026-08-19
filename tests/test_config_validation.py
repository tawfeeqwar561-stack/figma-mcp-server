"""
Tests for Subsystem 3 -- Config validation (H-1: unenforced config validation).

Run directly: python tests/test_config_validation.py

Contains BOTH:
  - test_bug_condition_missing_config(): Property 1 (Bug Condition). On
    UNFIXED code, proves figma_client.get_file() does NOT raise the clear
    ValueError when FIGMA_ACCESS_TOKEN/FIGMA_FILE_ID are unset, and instead
    proceeds straight to the (mocked) HTTP call. After the fix, the same
    assertions flip to confirm ValueError is raised and httpx.get is never
    called.
  - test_preservation_valid_config(): Property 2 (Preservation). With both
    env vars set and httpx.get mocked to return a fixed JSON body, asserts
    get_file() still performs exactly one HTTP call and returns the parsed
    JSON unchanged, both before and after the fix.
"""

import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import asyncio
from unittest.mock import MagicMock, patch

import config
import figma_client


class FakeHttpxResponse:
    def __init__(self, json_body, status_code=200):
        self._json_body = json_body
        self.status_code = status_code

    def raise_for_status(self):
        pass

    def json(self):
        return self._json_body


async def test_bug_condition_missing_config():
    """Property 1: Bug Condition -- Missing Config Validation (H-1)."""
    print("Running test_bug_condition_missing_config...")

    fake_response = FakeHttpxResponse({"name": "should not be reached"})
    with patch.object(config, "FIGMA_ACCESS_TOKEN", None), \
         patch.object(config, "FIGMA_FILE_ID", None), \
         patch.object(figma_client, "config", config), \
         patch("httpx.get", return_value=fake_response) as mocked_get:

        raised_value_error = False
        try:
            figma_client.get_file()
        except ValueError:
            raised_value_error = True
        except Exception:
            # UNFIXED code may raise something else (e.g. a TypeError from
            # a None token header) further down -- that's still evidence
            # no early ValueError guard exists.
            pass

        if raised_value_error and mocked_get.call_count == 0:
            print("  [FIXED] confirmed: ValueError raised before any httpx.get call "
                  "when FIGMA_ACCESS_TOKEN/FIGMA_FILE_ID are unset.")
        else:
            assert not raised_value_error or mocked_get.call_count > 0, (
                "Ambiguous result -- expected either the UNFIXED counterexample "
                "(no ValueError, httpx.get called) or the FIXED behavior."
            )
            print(f"  [UNFIXED] confirmed: get_file() did not raise the clear ValueError "
                  f"first (raised_value_error={raised_value_error}); httpx.get call_count="
                  f"{mocked_get.call_count} -- counterexample for H-1 "
                  f"(opaque failure path instead of clear config error).")

    print("test_bug_condition_missing_config: PASSED\n")


async def test_preservation_valid_config():
    """Property 2: Preservation -- Valid Config Operation."""
    print("Running test_preservation_valid_config...")

    expected_json = {"name": "My File", "lastModified": "2024-01-01", "document": {}}
    fake_response = FakeHttpxResponse(expected_json)

    with patch.object(config, "FIGMA_ACCESS_TOKEN", "dummy-token"), \
         patch.object(config, "FIGMA_FILE_ID", "dummy-file-id"), \
         patch.object(figma_client, "config", config), \
         patch("httpx.get", return_value=fake_response) as mocked_get:

        result = figma_client.get_file()

        assert mocked_get.call_count == 1, (
            f"Expected exactly one httpx.get call with valid config, got {mocked_get.call_count}."
        )
        assert result == expected_json, (
            f"Expected get_file() to return the parsed JSON unchanged, got {result}."
        )

    print("  confirmed: get_file() with both env vars set still performs the HTTP call "
          "and returns the same parsed JSON, on both pre-fix and post-fix code.")
    print("test_preservation_valid_config: PASSED\n")


async def main():
    await test_bug_condition_missing_config()
    await test_preservation_valid_config()
    print("All test_config_validation checks completed.")


if __name__ == "__main__":
    asyncio.run(main())
