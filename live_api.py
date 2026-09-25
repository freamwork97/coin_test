"""
Upbit Live Trading API — JWT-signed private API calls
======================================================
- Uses access_key + secret_key from environment / secrets file
- market buy/sell, limit buy/sell, cancel, order status, accounts
- Includes retry logic for rate limits (429)
"""

import os
import time
import uuid
import json
import logging
import requests
from typing import Dict, List, Optional, Any
import hashlib
from urllib.parse import urlencode

import jwt

logger = logging.getLogger(__name__)

ENV_PATH = os.path.expanduser("~/.openclaw/secrets/upbit.env")


def _load_env(path: str = ENV_PATH) -> Dict[str, str]:
    env = {}
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
    return env


class LiveUpbitAPI:
    def __init__(self, env_path: str = ENV_PATH):
        env = _load_env(env_path)
        self.access_key = env.get("UPBIT_ACCESS_KEY", "")
        self.secret_key = env.get("UPBIT_SECRET_KEY", "")
        self.server_url = env.get("UPBIT_SERVER_URL", "https://api.upbit.com")
        self.base_url = f"{self.server_url}/v1"
        self.session = requests.Session()
        self.max_retries = 3
        self.retry_delay = 2.0  # seconds

        # --- Per-group request pacing -------------------------------------
        # Upbit enforces BOTH a per-minute cap (candles: 600/min) and a
        # per-second cap. The bot requests ~96 candles/min — comfortably under
        # the minute cap — but fired them as a single burst (~70 req/s), which
        # tripped the per-second limiter and produced 429s on ~85% of calls
        # (measured 2026-09-25).
        #
        # Rather than guessing a fixed interval (which drifts as the universe
        # grows and as other clients share the IP quota), we pace adaptively
        # using the server's own `Remaining-Req: ...; sec=N` header. That
        # value IS the authoritative per-second quota signal, so reacting to
        # it keeps us under the limit without over-throttling.
        self._group_base_interval: Dict[str, float] = {
            "candles": 0.10,     # floor; only paid when the header is unavailable
            "ticker": 0.05,
            "orderbook": 0.05,
            "market": 0.05,
        }
        self._default_base_interval = 0.05
        # When the header reports this few requests left in the window, pause.
        self._quota_low_watermark = 1
        self._quota_pause = 0.25      # seconds to yield when quota is exhausted
        self._last_request_at: Dict[str, float] = {}   # group → monotonic ts
        self._throttle_lock = __import__('threading').Lock()

        # Rate limit tracking per group (from Remaining-Req header)
        self._rate_limits: Dict[str, int] = {}  # group → remaining this second
        self._rate_limit_lock = __import__('threading').Lock()

        # Accounts cache — 1s TTL to avoid redundant /accounts calls within a cycle
        self._accounts_cache: Optional[List[Dict]] = None
        self._accounts_cache_time: float = 0.0

    def _throttle(self, group: str):
        """Pace requests in a rate-limit group so a cycle's calls are spread
        out instead of bursting into the per-second limiter.

        Strategy: a small base interval keeps requests from arriving back to
        back; then, if the server's `Remaining-Req: ...; sec=N` header shows
        the per-second quota nearly exhausted, yield until it refills. The
        header is the authoritative signal, so this neither over-throttles a
        healthy connection nor ignores a real limit.
        """
        base = self._group_base_interval.get(group, self._default_base_interval)

        with self._throttle_lock:
            # 1. Enforce the minimum spacing within this group.
            if base > 0:
                now = time.monotonic()
                last = self._last_request_at.get(group, 0.0)
                wait = base - (now - last)
                if wait > 0:
                    time.sleep(wait)
                    now = time.monotonic()
                self._last_request_at[group] = now

            # 2. Yield while the per-second quota is (nearly) spent. Capped so
            #    a stale/misparsed header can never stall the bot indefinitely.
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                with self._rate_limit_lock:
                    remaining = self._rate_limits.get(group)
                if remaining is None or remaining > self._quota_low_watermark:
                    break
                time.sleep(self._quota_pause)
                self._last_request_at[group] = time.monotonic()

    def _update_rate_limit(self, resp):
        """Parse Remaining-Req header and update rate limit tracking."""
        header = resp.headers.get("Remaining-Req", "")
        if not header:
            return
        # Format: group=candle; min=1800; sec=29
        group = None
        for part in header.split(";"):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                k = k.strip()
                v = v.strip()
                if k == "group":
                    group = v
                elif k == "sec" and group:
                    with self._rate_limit_lock:
                        self._rate_limits[group] = int(v)

    def _auth_headers(self, query: Optional[Dict] = None) -> Dict[str, str]:
        payload = {
            "access_key": self.access_key,
            "nonce": str(uuid.uuid4()),
        }
        if query:
            qs = urlencode(query)
            m = hashlib.sha512(qs.encode()).hexdigest()
            payload["query_hash"] = m
            payload["query_hash_alg"] = "SHA512"

        jwt_token = jwt.encode(payload, self.secret_key)
        return {"Authorization": f"Bearer {jwt_token}"}

    def _request(self, method: str, endpoint: str, params: Optional[Dict] = None,
                 data: Optional[Dict] = None, auth: bool = True,
                 rate_limit_group: Optional[str] = None) -> Any:
        url = f"{self.base_url}{endpoint}"
        headers = {}
        if auth:
            headers = self._auth_headers(params if method == "GET" else data)
        headers["Content-Type"] = "application/json"

        for attempt in range(self.max_retries):
            try:
                # Space requests out per rate-limit group (see _throttle).
                self._throttle(rate_limit_group or "default")

                if method == "GET":
                    resp = self.session.get(url, params=params, headers=headers, timeout=15)
                elif method == "POST":
                    resp = self.session.post(url, json=data, headers=headers, timeout=15)
                elif method == "DELETE":
                    resp = self.session.delete(url, params=params, headers=headers, timeout=15)
                else:
                    raise ValueError(f"Unknown method: {method}")

                # Track rate limit from response header (for monitoring, not blocking)
                self._update_rate_limit(resp)

                if resp.status_code == 429:
                    # Honour the server's own signal when available, else back
                    # off. With proper pacing upstream this should rarely fire;
                    # when it does, the limiter needs a real pause, so this
                    # waits longer than the old flat 2s.
                    ra = resp.headers.get("Retry-After")
                    try:
                        wait = float(ra) if ra else self.retry_delay * (attempt + 1)
                    except (TypeError, ValueError):
                        wait = self.retry_delay * (attempt + 1)
                    logger.warning("Rate limit hit (429) — attempt %d/%d, waiting %.1fs",
                                   attempt + 1, self.max_retries, wait)
                    time.sleep(wait)
                    continue

                if resp.status_code == 418:
                    logger.error("IP/Account blocked (418) — waiting 60s before retry")
                    time.sleep(60)
                    continue

                resp.raise_for_status()
                return resp.json()

            except requests.exceptions.RequestException as e:
                if attempt < self.max_retries - 1:
                    logger.warning("Request failed (attempt %d): %s — retrying", attempt + 1, e)
                    time.sleep(self.retry_delay * (attempt + 1))
                else:
                    logger.error("Request failed after %d attempts: %s", self.max_retries, e)
                    return None

    # ------------------------------------------------------------------
    # Public API (same as paper bot)
    # ------------------------------------------------------------------
    def get_market_list(self) -> List[Dict]:
        return self._request("GET", "/market/all", {"isDetails": "true"}, auth=False,
                             rate_limit_group="market") or []

    def get_ticker(self, markets: List[str]) -> List[Dict]:
        market_str = ",".join(markets)
        return self._request("GET", "/ticker", {"markets": market_str}, auth=False,
                             rate_limit_group="ticker") or []

    def get_candles(self, market: str, interval: str = "60", count: int = 100) -> List[Dict]:
        endpoint = f"/candles/minutes/{interval}"
        return self._get_candles_paged(endpoint, market, count)

    def get_day_candles(self, market: str, count: int = 100) -> List[Dict]:
        return self._get_candles_paged("/candles/days", market, count)

    def _get_candles_paged(self, endpoint: str, market: str, count: int) -> List[Dict]:
        """Fetch candles, paginating with the `to` parameter when count > 200
        (Upbit caps each request at 200 candles). Returns newest-first."""
        if count <= 200:
            return self._request("GET", endpoint, {"market": market, "count": count},
                                 auth=False, rate_limit_group="candles") or []

        results: List[Dict] = []
        seen = set()
        to = None
        while len(results) < count:
            params = {"market": market, "count": 200}
            if to:
                params["to"] = to
            chunk = self._request("GET", endpoint, params, auth=False, rate_limit_group="candles") or []
            new = [c for c in chunk if c.get("candle_date_time_utc") not in seen]
            if not new:
                break
            for c in new:
                seen.add(c.get("candle_date_time_utc"))
            results.extend(new)
            if len(chunk) < 200:
                break  # no more history available
            to = results[-1]["candle_date_time_utc"] + "Z"
            time.sleep(0.06)
        return results[:count]

    def get_orderbook(self, markets: List[str]) -> List[Dict]:
        market_str = ",".join(markets)
        return self._request("GET", "/orderbook", {"markets": market_str}, auth=False,
                             rate_limit_group="orderbook") or []

    # ------------------------------------------------------------------
    # Private API — Account
    # ------------------------------------------------------------------
    def get_accounts(self) -> List[Dict]:
        """Get all balances. Cached with 1s TTL to avoid redundant calls within a cycle."""
        import time as _time
        now = _time.time()
        if self._accounts_cache is not None and (now - self._accounts_cache_time) < 1.0:
            return self._accounts_cache
        result = self._request("GET", "/accounts") or []
        self._accounts_cache = result
        self._accounts_cache_time = now
        return result

    def get_krw_balance(self) -> float:
        """Get available KRW balance."""
        accounts = self.get_accounts()
        for acc in accounts:
            if acc["currency"] == "KRW":
                return float(acc["balance"])
        return 0.0

    # ------------------------------------------------------------------
    # Private API — Orders
    # ------------------------------------------------------------------
    def market_buy(self, market: str, amount_krw: float) -> Optional[Dict]:
        """
        Market buy — spend exactly `amount_krw` KRW.
        Returns order info dict or None on failure.
        """
        data = {
            "market": market,
            "side": "bid",
            "ord_type": "price",
            "price": str(amount_krw),
        }
        logger.info("LIVE MARKET BUY  %s  amount=₩%.0f", market, amount_krw)
        result = self._request("POST", "/orders", data=data)
        if result:
            logger.info("  → order_id=%s", result.get("uuid"))
        return result

    def market_sell(self, market: str, volume: float) -> Optional[Dict]:
        """
        Market sell — sell exactly `volume` units.
        Returns order info dict or None on failure.
        """
        data = {
            "market": market,
            "side": "ask",
            "ord_type": "market",
            "volume": str(volume),
        }
        logger.info("LIVE MARKET SELL  %s  vol=%.6f", market, volume)
        result = self._request("POST", "/orders", data=data)
        if result:
            logger.info("  → order_id=%s", result.get("uuid"))
        return result

    def limit_buy(self, market: str, volume: float, price: float) -> Optional[Dict]:
        data = {
            "market": market,
            "side": "bid",
            "ord_type": "limit",
            "volume": str(volume),
            "price": str(price),
        }
        logger.info("LIVE LIMIT BUY  %s  vol=%.6f  price=%.0f", market, volume, price)
        return self._request("POST", "/orders", data=data)

    def limit_sell(self, market: str, volume: float, price: float) -> Optional[Dict]:
        data = {
            "market": market,
            "side": "ask",
            "ord_type": "limit",
            "volume": str(volume),
            "price": str(price),
        }
        logger.info("LIVE LIMIT SELL  %s  vol=%.6f  price=%.0f", market, volume, price)
        return self._request("POST", "/orders", data=data)

    def get_order(self, order_id: str) -> Optional[Dict]:
        return self._request("GET", "/order", {"uuid": order_id})

    def get_open_orders(self, market: Optional[str] = None) -> List[Dict]:
        params = {"state": "wait"}
        if market:
            params["market"] = market
        return self._request("GET", "/orders", params) or []

    def cancel_order(self, order_id: str) -> Optional[Dict]:
        return self._request("DELETE", "/order", {"uuid": order_id})

    def get_order_chance(self, market: str) -> Optional[Dict]:
        return self._request("GET", "/orders/chance", {"market": market})

    # ------------------------------------------------------------------
    # Safety: get exact position from exchange
    # ------------------------------------------------------------------
    def get_positions_from_exchange(self) -> Dict[str, float]:
        """
        Returns {market: volume} for all non-KRW balances.
        """
        positions = {}
        accounts = self.get_accounts()
        for acc in accounts:
            curr = acc["currency"]
            if curr == "KRW":
                continue
            bal = float(acc["balance"]) + float(acc.get("locked", 0))
            if bal > 0:
                market = f"KRW-{curr}"
                positions[market] = bal
        return positions

    def get_average_buy_price(self, market: str) -> Optional[float]:
        """
        Get average buy price for a specific market from accounts.
        """
        currency = market.replace("KRW-", "")
        accounts = self.get_accounts()
        for acc in accounts:
            if acc["currency"] == currency:
                avg = acc.get("avg_buy_price", "0")
                if avg and float(avg) > 0:
                    return float(avg)
        return None
