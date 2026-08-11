"""
Centralized configuration loader.
Reads environment variables from .env and validates required values.
"""

import os
from dotenv import load_dotenv

load_dotenv()

FIGMA_ACCESS_TOKEN: str | None = os.getenv("FIGMA_ACCESS_TOKEN")
FIGMA_FILE_ID: str | None = os.getenv("FIGMA_FILE_ID")
OLLAMA_BASE_URL: str = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")


def validate_config() -> None:
    """Raise a clear error if required config is missing."""
    if not FIGMA_ACCESS_TOKEN:
        raise ValueError(
            "FIGMA_ACCESS_TOKEN is not set. Check your .env file."
        )
    if not FIGMA_FILE_ID:
        raise ValueError(
            "FIGMA_FILE_ID is not set. Check your .env file."
        )