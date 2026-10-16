"""Offline tests: Channel3, PayPal and Claude are all mocked."""
import json
from decimal import Decimal
from types import SimpleNamespace as NS

import httpx
import pytest

from channel3_client import Channel3Client
from paypal_sandbox import SANDBOX_BASE_URL, PayPalSandbox
from shopper import Cart, Shopper

PRODUCT = {
    "id": "p1",
    "title": "Trail Jacket",
    "brands": [{"id": "b1", "name": "Acme"}],
    "offers": [
        {"url": "https://a.com/j", "domain": "a.com", "price": {"price": 120.0, "currency": "USD"}, "availability": "InStock"},
        {"url": "https://b.com/j", "domain": "b.com", "price": {"price": 99.5, "currency": "USD"}, "availability": "InStock"},
        {"url": "https://c.com/j", "domain": "c.com", "price": {"price": 50.0, "currency": "USD"}, "availability": "OutOfStock"},
    ],
}


def c3_handler(seen):
    def handle(req: httpx.Request):
        seen.append(req)
        assert req.headers["x-api-key"] == "k"
        if req.url.path == "/v1/search":
            return httpx.Response(200, json={"products": [PRODUCT], "next_page_token": None})
        if req.url.path == "/v1/products/p1":
            return httpx.Response(200, json=PRODUCT)
        return httpx.Response(404, json={"detail": "nope"})
    return handle


def pp_handler(seen, order_status="APPROVED"):
    def handle(req: httpx.Request):
        seen.append(req)
        assert str(req.url).startswith(SANDBOX_BASE_URL)
        p = req.url.path
        if p == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "T", "expires_in": 3600})
        assert req.headers["Authorization"] == "Bearer T"
        if p == "/v2/checkout/orders" and req.method == "POST":
            return httpx.Response(201, json={"id": "O1", "status": "PAYER_ACTION_REQUIRED",
                                             "links": [{"rel": "payer-action", "href": "https://sandbox.paypal.com/approve/O1"}]})
        if p == "/v2/checkout/orders/O1" and req.method == "GET":
            return httpx.Response(200, json={"id": "O1", "status": order_status})
        if p == "/v2/checkout/orders/O1/capture":
            return httpx.Response(201, json={"id": "O1", "status": "COMPLETED", "purchase_units": [
                {"payments": {"captures": [{"id": "C1", "status": "COMPLETED", "amount": {"value": "199.00"}}]}}]})
        return httpx.Response(404, json={"message": "nope"})
    return handle


def make(confirm=True, budget="250", order_status="APPROVED", llm=None):
    c3_seen, pp_seen = [], []
    c3 = Channel3Client(api_key="k", country="US", currency="USD",
                        http=httpx.Client(base_url="https://api.trychannel3.com", transport=httpx.MockTransport(c3_handler(c3_seen))))
    pp = PayPalSandbox(client_id="id", client_secret="s",
                       http=httpx.Client(base_url=SANDBOX_BASE_URL, transport=httpx.MockTransport(pp_handler(pp_seen, order_status))))
    s = Shopper(c3, pp, Cart(Decimal(budget), "USD"), confirm=lambda _: confirm, llm=llm or object(), log=lambda _: None)
    return s, c3_seen, pp_seen


def test_search_sends_filters_and_summarizes():
    s, c3_seen, _ = make()
    out = s.search_products("jacket", max_price=150, retailers=["a.com"], on_sale=True)
    body = json.loads(c3_seen[0].content)
    assert body["filters"] == {"price": {"min_price": None, "max_price": 150}, "website_ids": ["a.com"], "sale": "on_sale"}
    assert body["config"] == {"country": "US", "currency": "USD"}
    offers = out["products"][0]["offers"]
    assert offers[0]["retailer"] == "b.com"          # cheapest in-stock first
    assert offers[-1]["availability"] == "OutOfStock"  # out-of-stock last


def test_add_to_cart_uses_fetched_price_and_cheapest_in_stock():
    s, _, _ = make()
    out = s.add_to_cart("p1", quantity=2)
    assert out["retailer"] == "b.com" and out["unit_price"] == "99.50"
    assert s.cart.total == Decimal("199.00")


def test_add_to_cart_respects_retailer_and_stock():
    s, _, _ = make()
    assert "error" in s.add_to_cart("p1", retailer="c.com")  # out of stock
    assert s.add_to_cart("p1", retailer="a.com")["unit_price"] == "120.00"


def test_budget_enforced():
    s, _, _ = make(budget="150")
    assert "Over budget" in s.add_to_cart("p1", quantity=2)["error"]
    assert not s.cart.lines


def test_checkout_requires_confirmation():
    s, _, pp_seen = make(confirm=False)
    s.add_to_cart("p1")
    assert s.checkout()["cancelled"]
    assert pp_seen == []  # nothing sent to PayPal


def test_full_sandbox_payment_flow():
    s, _, pp_seen = make()
    s.add_to_cart("p1", quantity=2)
    order = s.checkout()
    assert order["order_id"] == "O1" and "sandbox.paypal.com" in order["approval_url"]
    body = json.loads(next(r for r in pp_seen if r.url.path == "/v2/checkout/orders").content)
    pu = body["purchase_units"][0]
    assert pu["amount"]["value"] == "199.00"
    assert pu["amount"]["breakdown"]["item_total"]["value"] == "199.00"
    assert pu["items"][0]["quantity"] == "2" and pu["items"][0]["unit_amount"]["value"] == "99.50"
    result = s.complete_payment("O1")
    assert result["status"] == "COMPLETED" and not s.cart.lines


def test_capture_blocked_until_approved():
    s, _, _ = make(order_status="PAYER_ACTION_REQUIRED")
    s.add_to_cart("p1")
    s.checkout()
    assert "hasn't approved" in s.complete_payment("O1")["error"]


def test_agent_loop_runs_tools_and_returns_text():
    calls = []
    script = [
        NS(stop_reason="tool_use", content=[NS(type="tool_use", id="t1", name="search_products", input={"query": "jacket"})]),
        NS(stop_reason="tool_use", content=[NS(type="tool_use", id="t2", name="add_to_cart", input={"product_id": "p1"})]),
        NS(stop_reason="end_turn", content=[NS(type="text", text="Added the Trail Jacket for $99.50.")]),
    ]

    class FakeLLM:
        class messages:
            @staticmethod
            def create(**kw):
                calls.append(kw)
                return script[len(calls) - 1]

    s, _, _ = make(llm=FakeLLM())
    assert s.send("buy me a jacket") == "Added the Trail Jacket for $99.50."
    assert s.cart.total == Decimal("99.50")
    tool_results = s.messages[-2]["content"]  # results sent before the final reply
    assert tool_results[0]["tool_use_id"] == "t2" and not tool_results[0]["is_error"]


def test_unknown_tool_is_error():
    s, _, _ = make()
    out, is_err = s.run_tool("delete_everything", {})
    assert is_err


def test_return_page_serves_and_notifies():
    import urllib.request
    import return_page

    events = []
    server = return_page.start(0, lambda k, t: events.append((k, t)))  # port 0 = any free port
    port = server.server_address[1]
    try:
        page = urllib.request.urlopen(f"http://127.0.0.1:{port}/paypal/return?token=ABC&PayerID=X").read().decode()
        assert "Payment approved" in page and "ABC" in page
        page = urllib.request.urlopen(f"http://127.0.0.1:{port}/paypal/cancel?token=<b>").read().decode()
        assert "Payment cancelled" in page and "&lt;b&gt;" in page  # escaped
        assert events == [("return", "ABC"), ("cancel", "<b>")]
    finally:
        server.shutdown()


def test_example_com_placeholder_replaced(monkeypatch):
    import return_page
    import shopper
    monkeypatch.setattr(return_page, "start", lambda port, notify: object())  # don't bind a real port
    monkeypatch.setenv("PAYPAL_RETURN_URL", "https://example.com/paypal/return")
    monkeypatch.setenv("SHOPPER_RETURN_PORT", "8799")
    ret, can = shopper.setup_return_page()
    assert ret == "http://localhost:8799/paypal/return" and can.endswith("/paypal/cancel")
