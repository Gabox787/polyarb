import logging
from dataclasses import dataclass

from market_scanner import PolyMarket
from nlp_engine import Signal
from config import MIN_EDGE

log = logging.getLogger(__name__)


@dataclass
class TradeDecision:
    market: PolyMarket
    side: str           # "YES" | "NO"
    edge: float         # expected edge 0..1
    reason: str


def _expected_price_shift(sentiment: float) -> float:
    """
    Rough model: strong positive news → YES should be ~sentiment*0.15 higher.
    Returns a signed expected shift in probability.
    """
    return sentiment * 0.15


def _compute_edge(market: PolyMarket, side: str, shift: float) -> float:
    """
    Edge = (fair_price - market_price) / market_price
    We estimate fair_price = current_price + shift
    """
    if side == "YES":
        current = market.yes_price
    else:
        current = market.no_price

    fair = max(0.01, min(0.99, current + shift))
    if current <= 0:
        return 0.0
    return (fair - current) / current


# Keywords that must appear in market question for it to be crypto-relevant
CRYPTO_MARKET_KEYWORDS = [
    "bitcoin", "btc", "ethereum", "eth", "crypto", "solana", "sol",
    "xrp", "ripple", "bnb", "binance", "coinbase", "blockchain",
    "altcoin", "defi", "nft", "stablecoin", "usdc", "usdt",
    "zcash", "zec", "hyperliquid", "polymarket",
]

def _is_crypto_market(question: str) -> bool:
    q = question.lower()
    return any(kw in q for kw in CRYPTO_MARKET_KEYWORDS)


def _is_5m_market(question: str) -> bool:
    q = question.lower()
    return any(kw in q for kw in ["up or down 5m", "btc updown", "5m", "5 min"])


def route(signal: Signal, markets: list[PolyMarket]) -> list[TradeDecision]:
    """
    Given an NLP signal and a list of candidate markets,
    return a list of TradeDecision sorted by edge descending.
    """
    if not signal.is_tradeable or not markets:
        return []

    # Filter to only crypto-relevant markets — never trade news on unrelated markets
    crypto_markets = [m for m in markets if _is_crypto_market(m.question)]
    if not crypto_markets:
        log.info("No crypto markets found for signal %s — skipping", signal.coin)
        return []
    markets = crypto_markets

    # Для GENERAL сигналов берём только BTC рынки как прокси всего крипто-рынка
    if signal.coin == "GENERAL":
        btc_markets = [m for m in markets if any(
            kw in m.question.lower() for kw in ["bitcoin", "btc"]
        )]
        if btc_markets:
            markets = btc_markets

    # Приоритет: 5-минутные BTC рынки
    five_min_markets = [m for m in markets if _is_5m_market(m.question)]
    if five_min_markets:
        log.info("Using %d 5m BTC markets (priority)", len(five_min_markets))
        markets = five_min_markets

    shift = _expected_price_shift(signal.sentiment)
    decisions = []

    for market in markets:
        # skip markets with very thin pricing data
        if market.yes_price <= 0.01 or market.yes_price >= 0.99:
            continue

        is_5m = _is_5m_market(market.question)

        if signal.sentiment > 0:
            side = "YES"  # bullish → BTC вырастет → UP
            edge = _compute_edge(market, "YES", abs(shift))
            if is_5m:
                edge = max(edge, 0.06)  # 5m рынки всегда имеют минимальный edge
        else:
            side = "NO"   # bearish → BTC упадёт → DOWN
            edge = _compute_edge(market, "NO", abs(shift))
            if is_5m:
                edge = max(edge, 0.06)

        if edge < MIN_EDGE:
            log.debug("Skip %s — edge %.3f < min %.3f",
                      market.question[:40], edge, MIN_EDGE)
            continue

        # prefer stale markets (MM slow to reprice)
        staleness_bonus = min(0.05, market.last_trade_age / 1000)
        effective_edge = edge + staleness_bonus

        reason = (
            f"sentiment={signal.sentiment:+.2f} | "
            f"shift={shift:+.3f} | "
            f"edge={edge:.3f} | "
            f"stale={market.last_trade_age:.0f}s | "
            f"keywords={signal.matched_keywords[:3]}"
        )

        decisions.append(TradeDecision(market, side, effective_edge, reason))
        log.info("SIGNAL %s → %s | edge=%.3f | %s",
                 market.question[:40], side, effective_edge, signal.coin)

    decisions.sort(key=lambda d: d.edge, reverse=True)

    # Возвращаем топ-3 но с разными рынками (диверсификация)
    seen_markets = set()
    unique_decisions = []
    for d in decisions:
        if d.market.market_id not in seen_markets:
            seen_markets.add(d.market.market_id)
            unique_decisions.append(d)
        if len(unique_decisions) >= 3:
            break
    return unique_decisions
