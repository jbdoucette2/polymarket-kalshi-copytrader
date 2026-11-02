"""
Minimal Kalshi Trade API client for live mode (Create Order V2, fixed-point dollars).

Docs: https://docs.kalshi.com/getting_started/quick_start_authenticated_requests
      https://docs.kalshi.com/api-reference/orders/create-order-v2

Credentials come only from environment variables; nothing here is ever written to disk:
  KALSHI_API_KEY_ID        API key ID from kalshi.com -> Account & security -> API Keys
  KALSHI_PRIVATE_KEY_PATH  path to the downloaded private key file, or
  KALSHI_PRIVATE_KEY       the PEM text itself (for CI secrets)
  KALSHI_ENV               "prod" (default) or "demo" (paper money on Kalshi's demo exchange)
"""
import base64, datetime, json, os, uuid
from urllib.parse import urlparse
import requests

BASES = {"prod": "https://external-api.kalshi.com/trade-api/v2",
         "demo": "https://external-api.demo.kalshi.co/trade-api/v2"}

class KalshiError(Exception):
    pass

class KalshiClient:
    def __init__(self):
        from cryptography.hazmat.primitives import serialization
        self.key_id = os.environ.get("KALSHI_API_KEY_ID")
        pem = os.environ.get("KALSHI_PRIVATE_KEY")
        path = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
        if not self.key_id or not (pem or path):
            raise KalshiError("Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH (or KALSHI_PRIVATE_KEY).")
        data = pem.encode() if pem else open(os.path.expanduser(path), "rb").read()
        self.key = serialization.load_pem_private_key(data, password=None)
        self.base = BASES[os.environ.get("KALSHI_ENV", "prod")]
        self.s = requests.Session()

    def _sign(self, ts, method, path):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        msg = f"{ts}{method}{path.split('?')[0]}".encode()
        if isinstance(self.key, Ed25519PrivateKey):
            sig = self.key.sign(msg)
        else:
            sig = self.key.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                                 salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
        return base64.b64encode(sig).decode()

    def _req(self, method, endpoint, body=None):
        url = self.base + endpoint
        ts = str(int(datetime.datetime.now().timestamp() * 1000))
        headers = {"KALSHI-ACCESS-KEY": self.key_id, "KALSHI-ACCESS-TIMESTAMP": ts,
                   "KALSHI-ACCESS-SIGNATURE": self._sign(ts, method, urlparse(url).path),
                   "Content-Type": "application/json"}
        r = self.s.request(method, url, headers=headers, data=json.dumps(body) if body else None, timeout=30)
        if r.status_code >= 400:
            raise KalshiError(f"{method} {endpoint} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.text else {}

    def balance_usd(self):
        return self._req("GET", "/portfolio/balance").get("balance", 0) / 100

    def market(self, ticker):
        return self._req("GET", f"/markets/{ticker}").get("market", {})

    def _order(self, ticker, book_side, count, yes_price, reduce_only=False, client_order_id=None):
        body = {"ticker": ticker, "side": book_side, "count": str(int(count)),
                "price": f"{yes_price:.4f}", "time_in_force": "immediate_or_cancel",
                "self_trade_prevention_type": "taker_at_cross",
                "client_order_id": client_order_id or str(uuid.uuid4())}
        if reduce_only:
            body["reduce_only"] = True
        resp = self._req("POST", "/portfolio/events/orders", body)
        resp["_request"] = body
        return resp

    def buy(self, ticker, outcome_side, count, max_price, client_order_id=None):
        """Buy `count` contracts of YES or NO, paying at most `max_price` per contract (immediate-or-cancel).
        Returns (filled_count, avg_price_paid_per_contract_for_that_outcome, fee_total, order_id, raw)."""
        if outcome_side == "yes":
            r = self._order(ticker, "bid", count, max_price, client_order_id=client_order_id)
        else:  # buying NO at q == selling YES at 1 - q
            r = self._order(ticker, "ask", count, 1 - max_price, client_order_id=client_order_id)
        filled = float(r.get("fill_count") or 0)
        avg_yes = float(r.get("average_fill_price") or 0)
        paid = avg_yes if outcome_side == "yes" else (1 - avg_yes if filled else 0)
        fee = round(float(r.get("average_fee_paid") or 0) * filled, 2)
        return filled, paid, fee, r.get("order_id"), r

    def sell(self, ticker, outcome_side, count, min_price):
        """Close a YES or NO position, receiving at least `min_price` per contract (reduce-only IOC)."""
        if outcome_side == "yes":
            r = self._order(ticker, "ask", count, min_price, reduce_only=True)
        else:  # selling NO at q == buying YES at 1 - q
            r = self._order(ticker, "bid", count, 1 - min_price, reduce_only=True)
        filled = float(r.get("fill_count") or 0)
        avg_yes = float(r.get("average_fill_price") or 0)
        got = avg_yes if outcome_side == "yes" else (1 - avg_yes if filled else 0)
        fee = round(float(r.get("average_fee_paid") or 0) * filled, 2)
        return filled, got, fee, r.get("order_id"), r
