#!/usr/bin/env python3

from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
API_SCRIPT = ROOT / "accounting_api.py"

REQUIREMENTS = {
    "httpx": "httpx",
    "openai": "openai",
    "pymupdf": "pymupdf",
    "dotenv": "python-dotenv",
}


def check_dependencies() -> None:
    missing = [
        package
        for module, package in REQUIREMENTS.items()
        if importlib.util.find_spec(module) is None
    ]
    if not missing:
        return

    activate = "source .venv/bin/activate"
    if sys.platform == "win32":
        activate = r".venv\Scripts\activate"

    command = f"python3 {Path(__file__).name} {' '.join(sys.argv[1:])}".rstrip()
    print(f"Missing required package(s): {', '.join(missing)}\n", file=sys.stderr)

    if (ROOT / ".venv").is_dir():
        print(
            "This project has a virtualenv that is not active. Run:\n"
            f"  {activate}\n"
            f"  {command}",
            file=sys.stderr,
        )
    else:
        print(
            "Set up the environment first:\n"
            "  python3 -m venv .venv\n"
            f"  {activate}\n"
            "  pip install -r requirements.txt\n"
            f"  {command}",
            file=sys.stderr,
        )
    raise SystemExit(1)


check_dependencies()

import httpx

from invoice_intake.config import get_settings
from invoice_intake.pipeline import process_invoices


def api_is_up(url: str) -> bool:
    try:
        response = httpx.get(f"{url}/health", timeout=2)
        return response.status_code == 200
    except httpx.HTTPError:
        return False


def ensure_api_running(url: str) -> subprocess.Popen | None:
    if api_is_up(url):
        print(f"Accounting API already running at {url}")
        return None

    print(f"Starting accounting API from {API_SCRIPT.name} ...")
    proc = subprocess.Popen(
        [sys.executable, str(API_SCRIPT)],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    for _ in range(20):
        if api_is_up(url):
            print(f"Accounting API ready at {url}")
            return proc
        if proc.poll() is not None:
            output = proc.stdout.read() if proc.stdout else ""
            raise RuntimeError(f"Accounting API failed to start:\n{output}")
        time.sleep(0.25)

    proc.terminate()
    raise RuntimeError("Timed out waiting for accounting API to start")


def main() -> int:
    parser = argparse.ArgumentParser(description="Process invoices and register to accounting API")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Delete all registered invoices before processing",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Extract and verify only; do not POST to the API",
    )
    parser.add_argument(
        "--no-api-start",
        action="store_true",
        help="Do not auto-start accounting_api.py (fail if API is down)",
    )
    parser.add_argument(
        "--only",
        metavar="SUBSTRING",
        help="Process only invoices whose filename contains SUBSTRING (e.g. --only 09)",
    )
    args = parser.parse_args()

    try:
        settings = get_settings()
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1

    api_proc: subprocess.Popen | None = None
    started_api = False

    try:
        if args.no_api_start:
            if not api_is_up(settings.api_url):
                print(f"Accounting API is not reachable at {settings.api_url}", file=sys.stderr)
                return 1
        else:
            api_proc = ensure_api_running(settings.api_url)
            started_api = api_proc is not None

        results = process_invoices(
            settings, reset=args.reset, dry_run=args.dry_run, only=args.only
        )
        failed = sum(1 for r in results if r.status == "failed")
        return 1 if failed else 0
    except (RuntimeError, httpx.HTTPError) as exc:
        print(exc, file=sys.stderr)
        return 1
    finally:
        if started_api and api_proc is not None:
            api_proc.terminate()
            try:
                api_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                api_proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
