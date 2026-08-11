"""
Simulates the MCP server (controller role) sending a command
through the bridge to whatever plugin is currently connected.
"""

import asyncio
import json
import logging

import websockets

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def main() -> None:
    uri = "ws://localhost:8765"
    async with websockets.connect(uri) as websocket:
        await websocket.send(json.dumps({"role": "controller"}))
        logger.info("Connected and identified as controller")

        command = {
            "action": "create_rectangle",
            "payload": {
                "x": 100,
                "y": 100,
                "width": 200,
                "height": 150,
                "color": {"r": 0.2, "g": 0.6, "b": 1.0},
            },
        }
        await websocket.send(json.dumps(command))
        logger.info("Command sent: %s", command)


if __name__ == "__main__":
    asyncio.run(main())