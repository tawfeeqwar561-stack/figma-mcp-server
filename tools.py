"""
MCP tool definitions for Figma operations.
Each function here is registered as a tool in server.py.
"""

import logging
from typing import Any

import figma_client

logger = logging.getLogger(__name__)


def get_file_overview(file_id: str | None = None) -> dict[str, Any]:
    """
    Get a summarized overview of a Figma file's structure.

    Args:
        file_id: Figma file key. Defaults to the configured FIGMA_FILE_ID.

    Returns:
        A dict with the file name and a list of top-level pages/frames.
    """
    data = figma_client.get_file(file_id)
    document = data.get("document", {})
    pages = document.get("children", [])

    summary = {
        "file_name": data.get("name", "Unknown"),
        "last_modified": data.get("lastModified", "Unknown"),
        "pages": [
            {
                "name": page.get("name"),
                "type": page.get("type"),
                "child_count": len(page.get("children", [])),
            }
            for page in pages
        ],
    }
    logger.info("Built overview for file: %s", summary["file_name"])
    return summary