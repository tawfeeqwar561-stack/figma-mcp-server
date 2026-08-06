"""
Milestone 3: Local test client for the Figma MCP Server.
Connects to server.py as a subprocess and calls tools directly.
"""

import asyncio
import logging

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

server_params = StdioServerParameters(
    command="python",
    args=["server.py"],
)


async def main() -> None:
    """Connect to the server, list tools, and call echo()."""
    async with stdio_client(server_params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

            tools_response = await session.list_tools()
            tool_names = [tool.name for tool in tools_response.tools]
            logger.info("Available tools: %s", tool_names)
            assert "echo" in tool_names, "echo tool not found on server!"

            result = await session.call_tool(
                "echo", arguments={"message": "hello from test client", "shout": True}
            )
            logger.info("Result: %s", result.content[0].text)


if __name__ == "__main__":
    asyncio.run(main())