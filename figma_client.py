"""
Figma API client.
Wraps authenticated calls to the Figma REST API.
"""

import logging
from typing import Any

import httpx

import config

logger = logging.getLogger(__name__)

FIGMA_API_BASE = "https://api.figma.com/v1"


def _headers() -> dict[str, str]:
    """Build the auth header required by every Figma API call."""
    return {"X-Figma-Token": config.FIGMA_ACCESS_TOKEN}


def get_file(file_id: str | None = None) -> dict[str, Any]:
    """
    Fetch a Figma file's full document structure.

    Args:
        file_id: Figma file key. Defaults to FIGMA_FILE_ID from .env.

    Returns:
        Parsed JSON response as a dict.

    Raises:
        httpx.HTTPStatusError: If Figma returns a non-2xx response.
    """
    config.validate_config()
    file_id = file_id or config.FIGMA_FILE_ID
    url = f"{FIGMA_API_BASE}/files/{file_id}"

    logger.info("Fetching Figma file: %s", file_id)
    response = httpx.get(url, headers=_headers(), timeout=10.0)
    response.raise_for_status()

    logger.info("Fetched file successfully (status %s)", response.status_code)
    return response.json()