"""
MCP tool definitions for Figma operations.
Each function here is registered as a tool in server.py.
"""

import logging
from typing import Any

import bridge_client
import figma_client
from design_plan import DesignPlan
from plan_executor import execute_plan
from planner import get_planner

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


async def create_rectangle(
    x: int = 0,
    y: int = 0,
    width: int = 100,
    height: int = 100,
    color_r: float = 0.5,
    color_g: float = 0.5,
    color_b: float = 0.5,
) -> dict:
    """
    Create a single rectangle on the Figma canvas via the bridge/plugin pipeline.
    Kept for simple/manual use; generate_screen and generate_ui_from_prompt
    use the plan executor instead for anything with multiple elements.
    """
    payload = {
        "x": x, "y": y, "width": width, "height": height,
        "color": {"r": color_r, "g": color_g, "b": color_b},
    }
    return await bridge_client.send_figma_command("create_rectangle", payload)


async def execute_design_plan(plan: DesignPlan) -> dict:
    """Execute a full design plan through the recursive plan executor."""
    return await execute_plan(plan)


async def generate_from_prompt(prompt: str) -> dict:
    """Convert a natural-language prompt into a plan, then execute it."""
    planner = get_planner()
    plan = await planner.generate_plan(prompt)
    result = await execute_plan(plan)
    result["generated_plan"] = plan.model_dump()
    return result