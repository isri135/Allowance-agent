"""Tiny local web server for PayPal's return/cancel redirect.

After the buyer approves (or cancels) in PayPal, the browser lands on
http://localhost:<port>/paypal/return or /paypal/cancel. This shows a friendly
page and notifies the terminal.
"""
from __future__ import annotations

import html
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import parse_qs, urlparse

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{ --bg:#f6f7f9; --card:#fff; --text:#1d2330; --muted:#5b6475; --accent:{accent}; }}
  @media (prefers-color-scheme: dark) {{ :root {{ --bg:#14171c; --card:#1d2128; --text:#e8ebf0; --muted:#9aa3b2; }} }}
  body {{ margin:0; min-height:100vh; display:grid; place-items:center; background:var(--bg);
         color:var(--text); font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; padding:16px; }}
  .card {{ background:var(--card); max-width:440px; width:100%; padding:32px 28px; border-radius:16px;
          box-shadow:0 8px 30px rgba(0,0,0,.08); text-align:center; }}
  .icon {{ width:56px; height:56px; border-radius:50%; margin:0 auto 16px; display:grid; place-items:center;
          background:var(--accent); color:#fff; font-size:28px; font-weight:700; }}
  h1 {{ font-size:22px; margin:0 0 8px; }}
  p {{ margin:0 0 12px; color:var(--muted); }}
  code {{ background:rgba(127,127,127,.15); padding:2px 8px; border-radius:6px; color:var(--text); font-size:15px; }}
  .small {{ font-size:13px; margin-top:20px; }}
</style></head>
<body><main class="card">
  <div class="icon">{icon}</div>
  <h1>{title}</h1>
  <p>{message}</p>
  <p class="small">PayPal sandbox · order {order}<br>You can close this tab.</p>
</main></body></html>"""

APPROVED = dict(
    title="Payment approved",
    icon="✓",
    accent="#1a8d5f",
    message="Go back to your terminal and type <code>done</code> so the shopper can complete the payment.",
)
CANCELLED = dict(
    title="Payment cancelled",
    icon="✕",
    accent="#b8463a",
    message="Nothing was charged. In your terminal, tell the shopper <code>checkout again</code> to get a new link.",
)


def render(kind: str, order: str) -> str:
    info = APPROVED if kind == "return" else CANCELLED
    return PAGE.format(order=html.escape(order or "unknown"), **info)


def start(port: int, notify: Callable[[str, str], None]) -> ThreadingHTTPServer | None:
    """Start the server in a background thread. Returns None if the port is busy.
    notify(kind, order_token) is called with kind 'return' or 'cancel'."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlparse(self.path)
            kind = {"/paypal/return": "return", "/paypal/cancel": "cancel"}.get(url.path)
            if kind is None:
                self.send_response(404)
                self.end_headers()
                return
            token = parse_qs(url.query).get("token", [""])[0]
            try:
                notify(kind, token)
            except Exception:
                pass
            body = render(kind, token).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # keep the terminal clean
            pass

    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError:
        return None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
