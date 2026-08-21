from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

load_dotenv(ROOT / ".env")

INVOICES_DIR = ROOT / "invoices"
OUTPUT_DIR = ROOT / "output"


@dataclass(frozen=True)
class Settings:
    openai_api_key: str
    openai_model: str
    api_url: str
    api_key: str
    invoices_dir: Path
    output_dir: Path


def get_settings() -> Settings:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Copy .env.example to .env and add your key."
        )
    return Settings(
        openai_api_key=api_key,
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4o"),
        api_url=os.getenv("ACCOUNTING_API_URL", "http://localhost:8080").rstrip("/"),
        api_key=os.getenv("ACCOUNTING_API_KEY", "demo-key-1234"),
        invoices_dir=INVOICES_DIR,
        output_dir=OUTPUT_DIR,
    )
