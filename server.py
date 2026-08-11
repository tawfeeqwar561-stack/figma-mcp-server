"""
Figma MCP Server - Entry point.
Exposes tools for reading Figma files and generating UI via the
bridge/plugin pipeline, including full natural-language screen generation.
"""

import logging

from mcp.server.fastmcp import FastMCP

import tools
from design_plan import DesignPlan

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

mcp = FastMCP("figma-mcp-server")


@mcp.tool()
def ping() -> str:
    """Health-check tool. Returns 'pong' if the server is alive."""
    logger.info("ping tool called")
    return "pong"


@mcp.tool()
def echo(message: str, shout: bool = False) -> str:
    """
    Echo a message back to the caller.

    Args:
        message: The text to echo back.
        shout: If True, returns the message in uppercase.
    """
    logger.info("echo tool called with message=%r shout=%s", message, shout)
    return message.upper() if shout else message


@mcp.tool()
def get_figma_file_overview(file_id: str = "") -> dict:
    """
    Get a summarized overview of a Figma file: its name and its pages.

    Args:
        file_id: Optional Figma file key. If empty, uses the default
                  configured file from .env.
    """
    logger.info("get_figma_file_overview called with file_id=%r", file_id)
    return tools.get_file_overview(file_id or None)


@mcp.tool()
async def create_figma_rectangle(
    x: int = 0,
    y: int = 0,
    width: int = 100,
    height: int = 100,
    color_r: float = 0.5,
    color_g: float = 0.5,
    color_b: float = 0.5,
) -> dict:
    """
    Create a single rectangle on the currently open Figma canvas.

    Args:
        x: X position.
        y: Y position.
        width: Rectangle width in pixels.
        height: Rectangle height in pixels.
        color_r: Red channel, 0.0 to 1.0.
        color_g: Green channel, 0.0 to 1.0.
        color_b: Blue channel, 0.0 to 1.0.
    """
    logger.info("create_figma_rectangle called: x=%d y=%d w=%d h=%d", x, y, width, height)
    return await tools.create_rectangle(x, y, width, height, color_r, color_g, color_b)


@mcp.tool()
async def generate_screen(plan: DesignPlan) -> dict:
    """
    Generate a complete screen on Figma from a structured, possibly nested
    DesignPlan. Container elements (frame, component, group, component_set)
    can have children. Supports auto layout, effects, constraints, color
    styles, text styles, and variables.
    """
    logger.info("generate_screen called: %s", plan.screen_name)
    return await tools.execute_design_plan(plan)


@mcp.tool()
async def generate_ui_from_prompt(prompt: str) -> dict:
    """
    Generate a complete Figma screen from a single natural-language prompt,
    e.g. "a mobile login screen with email, password, and a blue button".

    Uses an LLM planner if ANTHROPIC_API_KEY is configured in .env,
    otherwise falls back to keyword-matched templates (try 'login' or
    'dashboard' in the prompt).
    """
    logger.info("generate_ui_from_prompt called: %r", prompt)
    return await tools.generate_from_prompt(prompt)


if __name__ == "__main__":
    logger.info("Starting Figma MCP Server...")
    mcp.run()