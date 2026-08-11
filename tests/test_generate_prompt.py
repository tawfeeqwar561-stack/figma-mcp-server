"""
Direct test of generate_ui_from_prompt, bypassing the Inspector's
own client-side timeout, to isolate where the real delay is.
"""

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import asyncio
import logging
import time

import tools

logging.basicConfig(level=logging.INFO)


async def main() -> None:
    prompt = "a signup screen with a name field, email field, password field, and a green Create Account button"
    print(f"Starting at {time.strftime('%H:%M:%S')}...", flush=True)

    result = await tools.generate_from_prompt(prompt)

    print(f"Finished at {time.strftime('%H:%M:%S')}", flush=True)
    print("Screen name:", result.get("screen_name"))
    print("Succeeded:", result.get("succeeded"), "/", result.get("total_nodes"))


if __name__ == "__main__":
    asyncio.run(main())