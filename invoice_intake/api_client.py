from __future__ import annotations

from typing import Any

import httpx


class AccountingClient:
    def __init__(self, base_url: str, api_key: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.headers = {"X-API-Key": api_key, "Content-Type": "application/json"}

    def health(self) -> dict[str, Any]:
        response = httpx.get(f"{self.base_url}/health", timeout=10)
        response.raise_for_status()
        return response.json()

    def get_partners(self) -> list[dict[str, Any]]:
        response = httpx.get(
            f"{self.base_url}/partners", headers=self.headers, timeout=10
        )
        response.raise_for_status()
        body = response.json()
        return body["data"]["partners"]

    def get_tax_codes(self) -> list[dict[str, Any]]:
        response = httpx.get(
            f"{self.base_url}/tax-codes", headers=self.headers, timeout=10
        )
        response.raise_for_status()
        body = response.json()
        return body["data"]["tax_codes"]

    def register_invoice(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = httpx.post(
            f"{self.base_url}/invoices",
            headers=self.headers,
            json=payload,
            timeout=30,
        )
        return response.json()

    def list_invoices(self) -> list[dict[str, Any]]:
        response = httpx.get(
            f"{self.base_url}/invoices", headers=self.headers, timeout=10
        )
        response.raise_for_status()
        return response.json()["data"]["invoices"]

    def clear_invoices(self) -> int:
        response = httpx.delete(
            f"{self.base_url}/invoices", headers=self.headers, timeout=10
        )
        response.raise_for_status()
        return response.json()["data"]["removed"]
