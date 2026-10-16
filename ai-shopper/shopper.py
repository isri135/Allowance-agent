#!/usr/bin/env python3
"""AI online shopper.

Claude plans and shops; Channel3 finds products; PayPal SANDBOX takes payment.

    python shopper.py                 # chat with the shopper
    python shopper.py "a waterproof hiking jacket under $150"

Safety rails (enforced in code, not by the model):
  * Prices in the cart come from Channel3, never from the model.
  * Cart total can't exceed SHOPPER_BUDGET.
  * Checkout and payment capture each need you to type "yes" in the terminal.
  * PayPal is hard-wired to the sandbox host, so no real money moves.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable

from channel3_client import Channel3Client, Channel3Error, summarize_product
from paypal_sandbox import PayPalError, PayPalSandbox

MODEL = os.environ.get("SHOPPER_MODEL", "claude-sonnet-5-5")
MAX_QTY_PER_ITEM = 10


# ----------------------------------------------------------------------- cart
@dataclass
class CartLine:
    product_id: str
    title: str
    retailer: str
    url: str
    unit_price: Decimal
    currency: str
    quantity: int

    @property
    def subtotal(self) -> Decimal:
        return self.unit_price * self.quantity


@dataclass
class Cart:
    budget: Decimal
    currency: str
    lines: dict[str, CartLine] = field(default_factory=dict)

    @property
    def total(self) -> Decimal:
        return sum((l.subtotal for l in self.lines.values()), Decimal("0"))

    def view(self) -> dict[str, Any]:
        return {
            "currency": self.currency,
            "budget": f"{self.budget:.2f}",
            "total": f"{self.total:.2f}",
            "remaining_budget": f"{self.budget - self.total:.2f}",
            "items": [
                {
                    "product_id": l.product_id,
                    "title": l.title,
                    "retailer": l.retailer,
                    "unit_price": f"{l.unit_price:.2f}",
                    "quantity": l.quantity,
                    "subtotal": f"{l.subtotal:.2f}",
                    "url": l.url,
                }
                for l in self.lines.values()
            ],
        }


# ---------------------------------------------------------------------- tools
TOOLS: list[dict[str, Any]] = [
    {
        "name": "search_products",
        "description": (
            "Search the Channel3 product catalog across thousands of retailers. Returns products, "
            "each with offers from different retailers (price, availability, link). Use several "
            "focused searches rather than one vague one."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to look for, e.g. 'merino wool hiking socks'"},
                "min_price": {"type": "number"},
                "max_price": {"type": "number"},
                "retailers": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional retailer domains to restrict to, e.g. ['rei.com']",
                },
                "on_sale": {"type": "boolean", "description": "Only products currently on sale"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 6},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_product",
        "description": "Get full, current details and all offers for one product by product_id.",
        "input_schema": {
            "type": "object",
            "properties": {"product_id": {"type": "string"}},
            "required": ["product_id"],
        },
    },
    {
        "name": "add_to_cart",
        "description": (
            "Add a product to the cart from a specific retailer's offer. The price is re-fetched from "
            "Channel3; you cannot set it. Fails if out of stock, in the wrong currency, or over budget."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "product_id": {"type": "string"},
                "retailer": {"type": "string", "description": "Offer domain, e.g. 'rei.com'. Omit for cheapest in-stock offer."},
                "quantity": {"type": "integer", "minimum": 1, "maximum": MAX_QTY_PER_ITEM, "default": 1},
            },
            "required": ["product_id"],
        },
    },
    {
        "name": "remove_from_cart",
        "description": "Remove a product from the cart.",
        "input_schema": {
            "type": "object",
            "properties": {"product_id": {"type": "string"}},
            "required": ["product_id"],
        },
    },
    {
        "name": "view_cart",
        "description": "Show cart contents, total and remaining budget.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "checkout",
        "description": (
            "Create a PayPal SANDBOX order for the whole cart. The human must confirm in the terminal. "
            "Returns an approval link the human opens to log in with a sandbox buyer account and approve."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "complete_payment",
        "description": (
            "After the human says they approved the payment in PayPal, capture it. "
            "The human must confirm in the terminal."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"order_id": {"type": "string"}},
            "required": ["order_id"],
        },
    },
]

SYSTEM_PROMPT = """You are a careful online personal shopper.

How you work:
- Clarify only what truly changes the purchase (size, budget, must-haves). Otherwise make sensible choices.
- Search with Channel3, compare a few strong candidates, and prefer in-stock, new, well-priced offers from reputable retailers.
- Explain picks briefly: why this product, which retailer, the price.
- Add items to the cart, then show the cart and ask the human to confirm before checkout.
- Checkout pays through PayPal SANDBOX (test money). After checkout, give the human the approval link and wait for them to say they approved, then call complete_payment.
- Be clear that a sandbox payment is a simulation: no real order is placed with the retailer. Share the retailer links so they can buy for real if they want.
- After complete_payment, report each capture's status exactly. If a capture is PENDING, say so, give the pending_reason, and do not call the payment complete.
- Never invent prices, stock or links; only report what tools return.

Cart currency: {currency}. Budget: {budget} {currency}."""


class Shopper:
    def __init__(
        self,
        channel3: Channel3Client,
        paypal: PayPalSandbox | None,
        cart: Cart,
        confirm: Callable[[str], bool],
        llm=None,
        model: str = MODEL,
        log: Callable[[str], None] = lambda s: print(s, file=sys.stderr),
    ):
        self.c3 = channel3
        self.paypal = paypal
        self.cart = cart
        self.confirm = confirm
        self.model = model
        self.log = log
        self.messages: list[dict[str, Any]] = []
        self.orders: dict[str, str] = {}  # order_id -> status
        if llm is None:
            import anthropic

            llm = anthropic.Anthropic()
        self.llm = llm

    # ---- tool implementations ----
    def search_products(self, query, min_price=None, max_price=None, retailers=None, on_sale=False, limit=6):
        products = self.c3.search(query, limit=min(int(limit), 10), min_price=min_price,
                                  max_price=max_price, retailers=retailers, on_sale=on_sale)
        return {"count": len(products), "products": [summarize_product(p) for p in products]}

    def get_product(self, product_id):
        return summarize_product(self.c3.get_product(product_id), max_offers=10)

    def add_to_cart(self, product_id, retailer=None, quantity=1):
        quantity = int(quantity)
        if not 1 <= quantity <= MAX_QTY_PER_ITEM:
            return {"error": f"quantity must be 1-{MAX_QTY_PER_ITEM}"}
        product = self.c3.get_product(product_id)  # fresh price + stock
        offers = [o for o in product.get("offers", []) if o.get("availability") == "InStock" and o.get("price")]
        if retailer:
            offers = [o for o in offers if o.get("domain", "").lower() == retailer.lower()]
        offers = [o for o in offers if o["price"].get("currency") == self.cart.currency]
        if not offers:
            return {"error": f"No in-stock {self.cart.currency} offer"
                              + (f" from {retailer}" if retailer else "") + " for this product."}
        offer = min(offers, key=lambda o: o["price"]["price"])
        unit = Decimal(str(offer["price"]["price"])).quantize(Decimal("0.01"))

        existing = self.cart.lines.get(product_id)
        new_total = self.cart.total - (existing.subtotal if existing else 0) + unit * quantity
        if new_total > self.cart.budget:
            return {"error": f"Over budget: cart would be {new_total:.2f} {self.cart.currency}, "
                             f"budget is {self.cart.budget:.2f}."}
        self.cart.lines[product_id] = CartLine(
            product_id=product_id, title=product.get("title", product_id), retailer=offer["domain"],
            url=offer.get("url", ""), unit_price=unit, currency=self.cart.currency, quantity=quantity,
        )
        return {"added": product.get("title"), "retailer": offer["domain"],
                "unit_price": f"{unit:.2f}", "cart": self.cart.view()}

    def remove_from_cart(self, product_id):
        if self.cart.lines.pop(product_id, None) is None:
            return {"error": "Not in cart"}
        return self.cart.view()

    def view_cart(self):
        return self.cart.view()

    def checkout(self):
        if not self.cart.lines:
            return {"error": "Cart is empty"}
        if self.paypal is None:
            return {"error": "PayPal sandbox credentials are not configured."}
        view = self.cart.view()
        summary = "\n".join(f"  {i['quantity']} x {i['title']} ({i['retailer']}) @ {i['unit_price']}"
                            for i in view["items"])
        prompt = (f"\nCreate a PayPal SANDBOX order?\n{summary}\n  Total: {view['total']} "
                  f"{self.cart.currency}\nType 'yes' to continue: ")
        if not self.confirm(prompt):
            return {"cancelled": True, "reason": "Human declined checkout."}
        items = [{"name": l.title, "unit_price": l.unit_price, "quantity": l.quantity, "sku": l.product_id}
                 for l in self.cart.lines.values()]
        order = self.paypal.create_order(items, self.cart.currency)
        self.orders[order["order_id"]] = order["status"]
        return {**order, "next_step": "Human opens approval_url, logs in with a sandbox PERSONAL account, "
                                      "approves, then tells you. Then call complete_payment."}

    def complete_payment(self, order_id):
        if order_id not in self.orders:
            return {"error": "Unknown order_id for this session."}
        status = self.paypal.get_order(order_id).get("status")
        if status != "APPROVED":
            return {"error": f"Order status is {status}; the buyer hasn't approved it yet."}
        if not self.confirm(f"\nCapture sandbox payment for order {order_id}? Type 'yes': "):
            return {"cancelled": True}
        result = self.paypal.capture_order(order_id)
        self.orders[order_id] = result.get("status")
        if result.get("status") == "COMPLETED":
            self.cart.lines.clear()
        return result

    # ---- loop ----
    def run_tool(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        fn = getattr(self, name, None)
        if name not in {t["name"] for t in TOOLS} or fn is None:
            return json.dumps({"error": f"unknown tool {name}"}), True
        self.log(f"  · {name}({json.dumps(args)[:120]})")
        try:
            return json.dumps(fn(**args), default=str), False
        except (Channel3Error, PayPalError) as e:
            return json.dumps({"error": str(e)}), True
        except TypeError as e:
            return json.dumps({"error": f"bad arguments: {e}"}), True

    def send(self, user_text: str, max_steps: int = 25) -> str:
        self.messages.append({"role": "user", "content": user_text})
        system = SYSTEM_PROMPT.format(currency=self.cart.currency, budget=f"{self.cart.budget:.2f}")
        for _ in range(max_steps):
            resp = self.llm.messages.create(model=self.model, max_tokens=4096, system=system,
                                            tools=TOOLS, messages=self.messages)
            self.messages.append({"role": "assistant", "content": resp.content})
            if resp.stop_reason != "tool_use":
                return "".join(b.text for b in resp.content if b.type == "text")
            results = []
            for block in resp.content:
                if block.type == "tool_use":
                    out, is_err = self.run_tool(block.name, block.input or {})
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": out, "is_error": is_err})
            self.messages.append({"role": "user", "content": results})
        return "(Stopped: too many steps in one turn.)"


# ------------------------------------------------------------------------ CLI
def load_dotenv(path: str = ".env") -> None:
    """Load KEY=value lines from .env into the environment (works on any OS).
    Variables already set in the shell win. Inline '# comments' are ignored."""
    full = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
    if not os.path.exists(full):
        return
    with open(full, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.split(" #", 1)[0].strip().strip('"').strip("'")
            if key.strip() and value:
                os.environ.setdefault(key.strip(), value)


def setup_return_page() -> tuple[str | None, str | None]:
    """Serve the 'payment approved' page on localhost unless custom URLs are set.
    Old example.com placeholders from .env are ignored."""
    ret, can = os.environ.get("PAYPAL_RETURN_URL"), os.environ.get("PAYPAL_CANCEL_URL")
    if ret and "example.com" not in ret:
        return ret, can
    import return_page

    port = int(os.environ.get("SHOPPER_RETURN_PORT", "8765"))

    def notify(kind: str, token: str) -> None:
        if kind == "return":
            print(f"\n\n[PayPal] Order {token} approved in the browser. Type 'done' to complete it.\nyou › ",
                  end="", flush=True)
        else:
            print(f"\n\n[PayPal] Order {token} was cancelled in the browser.\nyou › ", end="", flush=True)

    if return_page.start(port, notify) is None:
        print(f"Note: port {port} is busy, so PayPal will redirect to a placeholder page after approval. "
              f"Set SHOPPER_RETURN_PORT to another number to fix this.\n")
        return None, None
    base = f"http://localhost:{port}/paypal"
    return f"{base}/return", f"{base}/cancel"


def terminal_confirm(prompt: str) -> bool:
    try:
        return input(prompt).strip().lower() in {"y", "yes"}
    except EOFError:
        return False


def main(argv: list[str]) -> int:
    load_dotenv()
    currency = os.environ.get("SHOPPER_CURRENCY", "USD").upper()
    country = os.environ.get("SHOPPER_COUNTRY") or {"USD": "US", "CAD": "CA", "GBP": "GB", "AUD": "AU"}.get(currency)
    budget = Decimal(os.environ.get("SHOPPER_BUDGET", "200"))

    try:
        c3 = Channel3Client(country=country, currency=currency)
    except Channel3Error as e:
        print(f"Setup error: {e}")
        return 1
    return_url, cancel_url = setup_return_page()
    try:
        paypal = PayPalSandbox(return_url=return_url, cancel_url=cancel_url)
    except PayPalError as e:
        print(f"Note: {e}\nYou can still search and build a cart; checkout is disabled.\n")
        paypal = None

    shopper = Shopper(c3, paypal, Cart(budget=budget, currency=currency), confirm=terminal_confirm)
    print(f"AI shopper ready · budget {budget:.2f} {currency} · PayPal SANDBOX · Ctrl-D to quit\n")

    first = " ".join(argv).strip()
    while True:
        try:
            text = first or input("you › ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        first = ""
        if not text:
            continue
        print(f"\nshopper › {shopper.send(text)}\n")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
