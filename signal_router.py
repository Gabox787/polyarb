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


def route(signal: Signal, markets: list[PolyMarket]) -> list[TradeDecision]:
    """
    Given an NLP signal and a list of candidate markets,
    return a list of TradeDecision sorted by edge descending.
    """
    if not signal.is_tradeable or not markets:
        return []

    shift = _expected_price_shift(signal.sentiment)
    decisions = []

    for market in markets:
        # skip markets with very thin pricing data
        if market.yes_price <= 0.01 or market.yes_price >= 0.99:
            continue

        if signal.sentiment > 0:
            # bullish → buy YES (price should rise)
            side = "YES"
            edge = _compute_edge(market, "YES", abs(shift))
        else:
            # bearish → buy NO (YES price should fall)
            side = "NO"
            edge = _compute_edge(market, "NO", abs(shift))

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
    return decisions[:3]   # top-3 best opportunities only
