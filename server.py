"""
Figma MCP Server - Entry point.
Milestone 6: real Figma tool exposed over MCP.
"""

import logging
from mcp.server.fastmcp import FastMCP

import tools

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
    if shout:
        return message.upper()
    return message


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


if __name__ == "__main__":
    logger.info("Starting Figma MCP Server...")
    mcp.run()