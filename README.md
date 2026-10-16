# Allowance Agent: AI Shopper

Built for the PayPal hackathon. A terminal shopping agent. Tell it what you want; it searches with **Channel3**, compares retailer offers, builds a cart within your budget, and pays through **PayPal sandbox** (test money).

```
you › I need a waterproof hiking jacket, men's medium, under $150
  · search_products({"query": "men's waterproof hiking jacket", "max_price": 150})
  · get_product({"product_id": "2yh8WH5"})
shopper › Three good options... I'd pick the ... from rei.com at $129.
you › go with that
  · add_to_cart(...)
  · checkout()

Create a PayPal SANDBOX order?
  1 x ... (rei.com) @ 129.00
  Total: 129.00 USD
Type 'yes' to continue: yes
shopper › Approve the payment here: https://www.sandbox.paypal.com/checkoutnow?token=...
you › done
Capture sandbox payment for order 5O1...? Type 'yes': yes
shopper › Payment captured (COMPLETED).
```

## Setup

The code lives in the `ai-shopper/` folder; run every command from there.

1. `cd ai-shopper` and `pip install -r requirements.txt`
2. Copy `.env.example` to `.env` (same folder as `shopper.py`) and fill it in. The shopper loads it automatically on Windows, Mac and Linux. `.env` is gitignored, so your keys stay local.
   - **Anthropic key**: from console.anthropic.com. `SHOPPER_MODEL` picks the Claude model.
   - **Channel3 key**: from your Channel3 dashboard.
   - **PayPal sandbox app**: developer.paypal.com → Apps & Credentials → **Sandbox** → create app → copy Client ID and Secret.
   - **Sandbox buyer**: developer.paypal.com → Testing Tools → Sandbox Accounts. Use the **Personal** account's email/password when you open the approval link.
3. `python shopper.py` (or `python shopper.py "a birthday gift for a coffee lover under $60"`)

Run tests (no keys needed): `pytest -q`

## How it works

| File (in `ai-shopper/`) | Role |
|---|---|
| `shopper.py` | Claude tool-use loop, cart, guardrails, CLI |
| `channel3_client.py` | `POST /v1/search` and `GET /v1/products/{id}` |
| `paypal_sandbox.py` | OAuth token, create order, get order, capture (Orders v2) |
| `return_page.py` | Local confirmation page PayPal redirects to after approval |
| `test_shopper.py` | Tests (no keys or network needed) |

Tools the agent can call: `search_products`, `get_product`, `add_to_cart`, `remove_from_cart`, `view_cart`, `checkout`, `complete_payment`.

Payment flow: `checkout` creates a PayPal order (`intent: CAPTURE`) and returns an approval link → you approve in the browser with the sandbox buyer → PayPal redirects to a confirmation page the shopper serves on `localhost:8765` (`return_page.py`), and the terminal shows that the order was approved → type `done` and `complete_payment` checks the order is `APPROVED` and captures it.

## Guardrails (in code, not just the prompt)

- **Sandbox only.** The PayPal host is hard-coded to `api-m.sandbox.paypal.com`; there's no live switch.
- **Real prices.** `add_to_cart` re-fetches the product from Channel3 and uses that offer's price. The model can't set a price.
- **In stock, one currency.** Only in-stock offers in `SHOPPER_CURRENCY` can go in the cart.
- **Budget.** The cart can't exceed `SHOPPER_BUDGET`.
- **Human confirmation.** Both checkout and capture wait for you to type `yes`.

## What this does *not* do

A sandbox payment is a simulation. Nobody ships anything, and no order is placed with the retailer. The agent shows each retailer's link so you can buy for real yourself. Placing real retailer orders would need a checkout integration with each merchant (Channel3 has a Checkout product worth looking at) and live payment credentials.
