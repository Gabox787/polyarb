import re
from dataclasses import dataclass
from config import BULLISH_KEYWORDS, BEARISH_KEYWORDS, COIN_KEYWORDS


@dataclass
class Signal:
    coin: str           # "BTC", "ETH", etc. or "GENERAL"
    sentiment: float    # -1.0 .. +1.0
    urgency: str        # "LOW" | "MEDIUM" | "HIGH"
    headline: str
    source: str
    matched_keywords: list[str]

    @property
    def is_tradeable(self) -> bool:
        # LOW urgency торгуем только если сентимент сильный (>=0.5)
        # MEDIUM/HIGH торгуем при сентименте >=0.3
        if self.urgency == "LOW":
            return abs(self.sentiment) >= 0.5
        return abs(self.sentiment) >= 0.3

    def __str__(self):
        sign = "+" if self.sentiment > 0 else ""
        return (f"[{self.coin}] {sign}{self.sentiment:.2f} | "
                f"{self.urgency} | {self.headline[:60]}")


URGENCY_WORDS = {
    "HIGH":   ["breaking", "just in", "urgent", "flash", "alert",
               "crash", "surge", "all-time high", "ath", "hack",
               "exploit", "ban", "approved", "rejected"],
    "MEDIUM": ["report", "says", "announces", "confirms", "warns",
               "reveals", "launches", "files", "sues"],
}

AMPLIFIERS = ["massive", "huge", "major", "significant", "record",
              "biggest", "largest", "historic", "unprecedented"]

NEGATORS = ["not", "no", "never", "without", "denies", "denying",
            "false", "fake", "rumor", "unconfirmed"]


def analyze(headline: str, source: str = "") -> Signal | None:
    text = headline.lower()
    text_clean = re.sub(r"[^a-zа-я0-9 ]", " ", text)
    words = text_clean.split()

    # --- detect coin ---
    coin = "GENERAL"
    for ticker, kws in COIN_KEYWORDS.items():
        if any(kw in text_clean for kw in kws):
            coin = ticker
            break

    # --- count sentiment hits ---
    bull_hits = [kw for kw in BULLISH_KEYWORDS if kw in text_clean]
    bear_hits = [kw for kw in BEARISH_KEYWORDS if kw in text_clean]

    if not bull_hits and not bear_hits:
        return None  # neutral, skip

    raw_score = len(bull_hits) - len(bear_hits)

    # amplify
    amp_count = sum(1 for a in AMPLIFIERS if a in text_clean)
    if raw_score > 0:
        raw_score += amp_count * 0.5
    elif raw_score < 0:
        raw_score -= amp_count * 0.5

    # negate (e.g. "NOT approved")
    neg_count = sum(1 for n in NEGATORS if n in words)
    if neg_count % 2 == 1:  # odd negations flip the sign
        raw_score *= -1

    # normalise to [-1, +1]
    sentiment = max(-1.0, min(1.0, raw_score / 3.0))

    if abs(sentiment) < 0.1:
        return None

    # --- urgency ---
    urgency = "LOW"
    for lvl, kws in URGENCY_WORDS.items():
        if any(kw in text_clean for kw in kws):
            urgency = lvl
            break
    # boost urgency when sentiment is very strong
    if abs(sentiment) >= 0.7 and urgency == "LOW":
        urgency = "MEDIUM"

    matched = bull_hits + bear_hits
    return Signal(coin, round(sentiment, 3), urgency,
                  headline, source, matched)
