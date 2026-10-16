"""PayPal Orders v2 client, locked to the SANDBOX environment.

The base URL is hard-coded to api-m.sandbox.paypal.com and there is no switch
to live, so this agent can never move real money.

Docs: https://developer.paypal.com/docs/api/orders/v2/
"""
from __future__ import annotations

import os
import time
import uuid
from decimal import Decimal
from typing import Any

import httpx

SANDBOX_BASE_URL = "https://api-m.sandbox.paypal.com"


class PayPalError(RuntimeError):
    pass


def money(value: Decimal, currency: str) -> dict[str, str]:
    return {"currency_code": currency, "value": f"{value:.2f}"}


class PayPalSandbox:
    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        return_url: str | None = None,
        cancel_url: str | None = None,
        http: httpx.Client | None = None,
    ):
        self.client_id = client_id or os.environ.get("PAYPAL_SANDBOX_CLIENT_ID")
        self.client_secret = client_secret or os.environ.get("PAYPAL_SANDBOX_CLIENT_SECRET")
        if not (self.client_id and self.client_secret):
            raise PayPalError(
                "PAYPAL_SANDBOX_CLIENT_ID and PAYPAL_SANDBOX_CLIENT_SECRET must be set "
                "(sandbox app credentials from developer.paypal.com)"
            )
        self.return_url = return_url or os.environ.get("PAYPAL_RETURN_URL", "https://example.com/paypal/return")
        self.cancel_url = cancel_url or os.environ.get("PAYPAL_CANCEL_URL", "https://example.com/paypal/cancel")
        self.http = http or httpx.Client(base_url=SANDBOX_BASE_URL, timeout=30)
        self._token: str | None = None
        self._token_expiry = 0.0

    # --- auth -----------------------------------------------------------
    def _access_token(self) -> str:
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        resp = self.http.post(
            "/v1/oauth2/token",
            data={"grant_type": "client_credentials"},
            auth=(self.client_id, self.client_secret),
        )
        data = self._check(resp)
        self._token = data["access_token"]
        self._token_expiry = time.time() + int(data.get("expires_in", 3000))
        return self._token

    def _headers(self, idempotent: bool = False) -> dict[str, str]:
        h = {"Authorization": f"Bearer {self._access_token()}", "Content-Type": "application/json"}
        if idempotent:
            h["PayPal-Request-Id"] = str(uuid.uuid4())
        return h

    @staticmethod
    def _check(resp: httpx.Response) -> dict[str, Any]:
        if resp.status_code >= 400:
            try:
                body = resp.json()
                msg = body.get("message") or body.get("error_description") or body
                details = body.get("details")
                if details:
                    msg = f"{msg} {details}"
            except Exception:
                msg = resp.text
            debug_id = resp.headers.get("paypal-debug-id", "")
            raise PayPalError(f"PayPal HTTP {resp.status_code}: {msg} {debug_id}".strip())
        return resp.json() if resp.content else {}

    # --- orders ---------------------------------------------------------
    @staticmethod
    def build_order_body(items: list[dict[str, Any]], currency: str, return_url: str, cancel_url: str) -> dict[str, Any]:
        """items: [{name, unit_price (Decimal), quantity (int), sku}]"""
        pp_items = []
        total = Decimal("0")
        for it in items:
            unit = Decimal(it["unit_price"]).quantize(Decimal("0.01"))
            qty = int(it["quantity"])
            total += unit * qty
            pp_items.append(
                {
                    "name": it["name"][:127],
                    "sku": str(it.get("sku", ""))[:127],
                    "quantity": str(qty),
                    "unit_amount": money(unit, currency),
                    "category": "PHYSICAL_GOODS",
                }
            )
        return {
            "intent": "CAPTURE",
            "purchase_units": [
                {
                    "reference_id": f"shopper-{uuid.uuid4().hex[:12]}",
                    "description": "AI shopper sandbox order",
                    "amount": {
                        **money(total, currency),
                        "breakdown": {"item_total": money(total, currency)},
                    },
                    "items": pp_items,
                }
            ],
            "payment_source": {
                "paypal": {
                    "experience_context": {
                        "user_action": "PAY_NOW",
                        "shipping_preference": "NO_SHIPPING",
                        "return_url": return_url,
                        "cancel_url": cancel_url,
                    }
                }
            },
        }

    def create_order(self, items: list[dict[str, Any]], currency: str) -> dict[str, Any]:
        body = self.build_order_body(items, currency, self.return_url, self.cancel_url)
        data = self._check(self.http.post("/v2/checkout/orders", json=body, headers=self._headers(idempotent=True)))
        approve = next(
            (l["href"] for l in data.get("links", []) if l.get("rel") in ("payer-action", "approve")),
            None,
        )
        return {"order_id": data["id"], "status": data.get("status"), "approval_url": approve}

    def get_order(self, order_id: str) -> dict[str, Any]:
        return self._check(self.http.get(f"/v2/checkout/orders/{order_id}", headers=self._headers()))

    def capture_order(self, order_id: str) -> dict[str, Any]:
        data = self._check(
            self.http.post(f"/v2/checkout/orders/{order_id}/capture", json={}, headers=self._headers(idempotent=True))
        )
        captures = [
            c
            for pu in data.get("purchase_units", [])
            for c in (pu.get("payments") or {}).get("captures", [])
        ]
        return {
            "order_id": data.get("id"),
            "status": data.get("status"),
            "captures": [
                {
                    "capture_id": c.get("id"),
                    "status": c.get("status"),
                    # Why a capture is PENDING, e.g. RECEIVING_PREFERENCE_MANDATES_MANUAL_ACTION
                    "pending_reason": (c.get("status_details") or {}).get("reason"),
                    "amount": c.get("amount"),
                    "seller_receives": (c.get("seller_receivable_breakdown") or {}).get("net_amount"),
                }
                for c in captures
            ],
        }
