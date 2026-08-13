"""
Type your own prompt below, between the quotes, then run this file.
That's the only line you ever need to change.
"""

import asyncio
import logging

logging.basicConfig(level=logging.INFO)
import tools

# ============================================================
# EDIT THIS LINE — put whatever screen you want here:
MY_PROMPT = "a login screen with an email field, a password field, and a login button"
# ============================================================


async def main():
    print(f"\nGenerating: {MY_PROMPT}\n")
    result = await tools.generate_from_prompt(MY_PROMPT)
    print("\n--- RESULT ---")
    print("Screen name:", result.get("screen_name"))
    print("Succeeded:", result.get("succeeded"), "/", result.get("total_nodes"))
    print("Now check Figma (press Shift+1 to zoom to fit)")


if __name__ == "__main__":
    asyncio.run(main())
