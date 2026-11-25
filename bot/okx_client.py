"""
Minimal OKX v5 REST client. Credentials are read ONLY from environment
variables -- never hardcoded, so this file is safe to commit to a public
or private GitHub repo. When running in GitHub Actions, set these as
encrypted repository secrets:

    OKX_API_KEY
    OKX_SECRET_KEY
    OKX_PASSPHRASE

Demo-trading routing is controlled purely by the x-simulated-trading
header (set to "1" below) -- NOT by using a different key. Flip
OKX_SIMULATED=0 only if you deliberately intend to trade the live account
(not recommended for this project).
"""
import base64
import hashlib
import hmac
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone

OKX_BASE_URL = "https://www.okx.com"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


class OKXError(RuntimeError):
    pass


class OKXClient:
    def __init__(self, api_key=None, secret_key=None, passphrase=None, simulated=None):
        self.api_key = api_key or os.environ["OKX_API_KEY"]
        self.secret_key = secret_key or os.environ["OKX_SECRET_KEY"]
        self.passphrase = passphrase or os.environ["OKX_PASSPHRASE"]
        if simulated is None:
            simulated = os.environ.get("OKX_SIMULATED", "1")
        self.simulated = str(simulated)

    def _timestamp(self):
        now = datetime.now(timezone.utc)
        return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"

    def _sign(self, ts, method, path, body):
        msg = f"{ts}{method}{path}{body}"
        return base64.b64encode(
            hmac.new(self.secret_key.encode(), msg.encode(), hashlib.sha256).digest()
        ).decode()

    def _request(self, method, path, body_obj=None):
        body = json.dumps(body_obj) if body_obj else ""
        ts = self._timestamp()
        sig = self._sign(ts, method, path, body)
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "OK-ACCESS-KEY": self.api_key,
            "OK-ACCESS-SIGN": sig,
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json",
            "x-simulated-trading": self.simulated,
        }
        req = urllib.request.Request(
            OKX_BASE_URL + path,
            data=body.encode() if body else None,
            headers=headers,
            method=method,
        )
        try:
            resp = urllib.request.urlopen(req, timeout=20)
            data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raise OKXError(f"HTTP {e.code}: {e.read()[:500]}")
        if data.get("code") not in ("0", None):
            raise OKXError(f"{path} -> {data}")
        return data

    # -- public (no auth needed, but we sign anyway, OKX ignores it) --
    def ticker(self, inst_id):
        return self._request("GET", f"/api/v5/market/ticker?instId={inst_id}")["data"][0]

    def candles(self, inst_id, bar="4H", limit=30):
        return self._request(
            "GET", f"/api/v5/market/candles?instId={inst_id}&bar={bar}&limit={limit}"
        )["data"]

    def instrument(self, inst_id, inst_type="SWAP"):
        return self._request(
            "GET", f"/api/v5/public/instruments?instType={inst_type}&instId={inst_id}"
        )["data"][0]

    def funding_rate(self, inst_id):
        return self._request("GET", f"/api/v5/public/funding-rate?instId={inst_id}")["data"][0]

    # -- private (auth required) --
    def balance(self):
        return self._request("GET", "/api/v5/account/balance")["data"][0]

    def positions(self, inst_id=None):
        path = "/api/v5/account/positions"
        if inst_id:
            path += f"?instId={inst_id}"
        return self._request("GET", path)["data"]

    def set_leverage(self, inst_id, lever, mgn_mode="cross"):
        return self._request(
            "POST",
            "/api/v5/account/set-leverage",
            {"instId": inst_id, "lever": str(lever), "mgnMode": mgn_mode},
        )

    def place_order(self, inst_id, td_mode, side, sz, ord_type="market", cl_ord_id=None, tgt_ccy=None):
        body = {
            "instId": inst_id,
            "tdMode": td_mode,
            "side": side,
            "ordType": ord_type,
            "sz": str(sz),
        }
        if cl_ord_id:
            body["clOrdId"] = cl_ord_id
        if tgt_ccy:
            body["tgtCcy"] = tgt_ccy
        return self._request("POST", "/api/v5/trade/order", body)

    def order_detail(self, inst_id, ord_id):
        return self._request(
            "GET", f"/api/v5/trade/order?instId={inst_id}&ordId={ord_id}"
        )["data"][0]
