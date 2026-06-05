import asyncio
import logging
import time
from dataclasses import dataclass

import aiohttp

from config import POLYMARKET_GAMMA_API, POLYMARKET_CLOB_API

log = logging.getLogger(__name__)

COIN_MARKET_KEYWORDS = {
    "BTC":  ["bitcoin", "btc"],
    "ETH":  ["ethereum", "eth"],
    "SOL":  ["solana", "sol"],
    "BNB":  ["binance", "bnb"],
    "XRP":  ["xrp", "ripple"],
    "GENERAL": [],
}

# How old (seconds) a last-trade timestamp can be and still count as "stale"
STALE_THRESHOLD_SEC = 30


@dataclass
class PolyMarket:
    market_id: str
    question: str
    yes_price: float    # 0..1
    no_price: float
    volume_24h: float
    last_trade_age: float   # seconds since last trade
    condition_id: str


class MarketScanner:
    def __init__(self):
        self._session: aiohttp.ClientSession | None = None
        self._cache: dict[str, list[PolyMarket]] = {}  # coin -> markets
        self._cache_ts: float = 0
        self._cache_ttl: float = 60  # refresh every 60s

    async def start(self):
        self._session = aiohttp.ClientSession()

    async def stop(self):
        if self._session:
            await self._session.close()

    async def find_markets(self, coin: str) -> list[PolyMarket]:
        """Return open Polymarket markets related to coin."""
        await self._refresh_cache_if_needed()
        coin_up = coin.upper()
        return self._cache.get(coin_up, []) + self._cache.get("GENERAL", [])

    async def find_stale_markets(self, coin: str) -> list[PolyMarket]:
        """Markets where MM hasn't repriced recently — best arb targets."""
        markets = await self.find_markets(coin)
        stale = [m for m in markets if m.last_trade_age > STALE_THRESHOLD_SEC]
        stale.sort(key=lambda m: m.last_trade_age, reverse=True)
        return stale

    # ------------------------------------------------------------------ #
    async def _refresh_cache_if_needed(self):
        if time.time() - self._cache_ts < self._cache_ttl:
            return
        await self._refresh_cache()

    async def _refresh_cache(self):
        log.info("Refreshing Polymarket market cache…")
        raw = await self._fetch_gamma_markets()
        new_cache: dict[str, list[PolyMarket]] = {}

        for item in raw:
            try:
                pm = self._parse_market(item)
                if pm is None:
                    continue
                coin = self._classify_coin(pm.question)
                new_cache.setdefault(coin, []).append(pm)
            except Exception as e:
                log.debug("Market parse error: %s", e)

        self._cache = new_cache
        self._cache_ts = time.time()
        total = sum(len(v) for v in new_cache.values())
        log.info("Cache: %d markets across %d coins", total, len(new_cache))

    async def _fetch_gamma_markets(self) -> list[dict]:
        """Fetch active crypto markets from Gamma API."""
        url = f"{POLYMARKET_GAMMA_API}/markets"
        params = {
            "active": "true",
            "closed": "false",
            "tag_slug": "crypto",
            "limit": 100,
        }
        try:
            async with self._session.get(
                url, params=params,
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status != 200:
                    log.warning("Gamma API %d", resp.status)
                    return []
                data = await resp.json()
                # response may be list or {"markets": [...]}
                if isinstance(data, list):
                    return data
                return data.get("markets", data.get("data", []))
        except Exception as e:
            log.warning("Gamma fetch error: %s", e)
            return []

    def _parse_market(self, item: dict) -> PolyMarket | None:
        question = item.get("question", "") or item.get("title", "")
        if not question:
            return None

        # --- prices: try CLOB tokens first, then direct fields ---
        tokens = item.get("tokens", [])
        yes_price, no_price = 0.5, 0.5

        if len(tokens) >= 2:
            for tok in tokens:
                outcome = str(tok.get("outcome", "")).upper()
                price = float(tok.get("price", 0.5) or 0.5)
                if outcome == "YES":
                    yes_price = price
                elif outcome == "NO":
                    no_price = price
        else:
            yes_price = float(item.get("bestAsk", item.get("outcomePrices", [0.5])[0] if item.get("outcomePrices") else 0.5) or 0.5)
            no_price = 1.0 - yes_price

        # --- volume ---
        volume = float(item.get("volume24hr", item.get("volumeNum", 0)) or 0)

        # --- staleness ---
        last_trade_ts = item.get("lastTradeTime") or item.get("updatedAt") or ""
        try:
            from datetime import datetime, timezone
            import re
            # strip sub-second precision that Python can't parse
            ts_clean = re.sub(r"\.\d+", "", str(last_trade_ts))
            ts_clean = ts_clean.replace("Z", "+00:00")
            dt = datetime.fromisoformat(ts_clean)
            age = (datetime.now(timezone.utc) - dt).total_seconds()
        except Exception:
            age = 999.0

        return PolyMarket(
            market_id   = str(item.get("id", "")),
            question    = question,
            yes_price   = round(yes_price, 4),
            no_price    = round(no_price, 4),
            volume_24h  = volume,
            last_trade_age = age,
            condition_id = str(item.get("conditionId", "")),
        )

    @staticmethod
    def _classify_coin(question: str) -> str:
        q = question.lower()
        for coin, kws in COIN_MARKET_KEYWORDS.items():
            if coin == "GENERAL":
                continue
            if any(kw in q for kw in kws):
                return coin
        return "GENERAL"
