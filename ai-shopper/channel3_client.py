"""Thin Channel3 client: product search and product detail.

Docs: https://docs.trychannel3.com/api-reference/v1/search
"""
from __future__ import annotations

import os
from typing import Any

import httpx

BASE_URL = "https://api.trychannel3.com"


class Channel3Error(RuntimeError):
    pass


class Channel3Client:
    def __init__(
        self,
        api_key: str | None = None,
        country: str | None = None,
        currency: str | None = None,
        http: httpx.Client | None = None,
    ):
        self.api_key = api_key or os.environ.get("CHANNEL3_API_KEY")
        if not self.api_key:
            raise Channel3Error("CHANNEL3_API_KEY is not set")
        self.country = country
        self.currency = currency
        self.http = http or httpx.Client(base_url=BASE_URL, timeout=30)

    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self.api_key, "Content-Type": "application/json"}

    def _config(self) -> dict[str, Any]:
        cfg: dict[str, Any] = {}
        if self.country:
            cfg["country"] = self.country
        if self.currency:
            cfg["currency"] = self.currency
        return cfg

    @staticmethod
    def _check(resp: httpx.Response) -> dict[str, Any]:
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail")
            except Exception:
                detail = resp.text
            raise Channel3Error(f"Channel3 HTTP {resp.status_code}: {detail}")
        return resp.json()

    def search(
        self,
        query: str,
        limit: int = 8,
        min_price: float | None = None,
        max_price: float | None = None,
        retailers: list[str] | None = None,
        on_sale: bool = False,
    ) -> list[dict[str, Any]]:
        filters: dict[str, Any] = {}
        if min_price is not None or max_price is not None:
            filters["price"] = {"min_price": min_price, "max_price": max_price}
        if retailers:
            filters["website_ids"] = retailers  # accepts domains, e.g. "nike.com"
        if on_sale:
            filters["sale"] = "on_sale"

        body: dict[str, Any] = {"query": query, "limit": max(1, min(limit, 30))}
        if filters:
            body["filters"] = filters
        cfg = self._config()
        if cfg:
            body["config"] = cfg

        data = self._check(self.http.post("/v1/search", json=body, headers=self._headers()))
        return data.get("products", [])

    def get_product(self, product_id: str) -> dict[str, Any]:
        params = self._config()
        resp = self.http.get(f"/v1/products/{product_id}", params=params, headers=self._headers())
        return self._check(resp)


def summarize_product(p: dict[str, Any], max_offers: int = 4) -> dict[str, Any]:
    """Compact view of a Channel3 product for the model (keeps tokens down)."""
    offers = sorted(
        (o for o in p.get("offers", []) if o.get("price")),
        key=lambda o: (o.get("availability") != "InStock", o["price"].get("price", 1e12)),
    )
    return {
        "product_id": p.get("id"),
        "title": p.get("title"),
        "brands": [b.get("name") for b in p.get("brands", [])],
        "category": (p.get("category") or {}).get("title"),
        "key_features": (p.get("key_features") or [])[:5],
        "offers": [
            {
                "retailer": o.get("domain"),
                "price": o["price"].get("price"),
                "compare_at_price": o["price"].get("compare_at_price"),
                "currency": o["price"].get("currency"),
                "availability": o.get("availability"),
                "condition": o.get("condition"),
                "url": o.get("url"),
            }
            for o in offers[:max_offers]
        ],
    }
