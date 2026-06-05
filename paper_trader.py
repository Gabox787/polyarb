import asyncio
import json
import logging
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Literal

from config import INITIAL_BALANCE, BET_SIZE, MIN_EDGE, MAX_OPEN_POSITIONS
from market_scanner import PolyMarket
from nlp_engine import Signal

log = logging.getLogger(__name__)

Side = Literal["YES", "NO"]
State = Literal["OPEN", "CLOSED_WIN", "CLOSED_LOSS", "CLOSED_NEUTRAL"]

SAVE_FILE = Path("paper_trades.json")


@dataclass
class Position:
    pos_id:     str
    market_id:  str
    question:   str
    side:       Side
    entry_price: float
    size_usdc:  float
    open_ts:    float = field(default_factory=time.time)
    close_ts:   float = 0.0
    close_price: float = 0.0
    pnl:        float = 0.0
    state:      State = "OPEN"
    news_headline: str = ""
    coin:       str = ""
    sentiment:  float = 0.0

    @property
    def shares(self) -> float:
        """How many outcome shares we hold."""
        return self.size_usdc / self.entry_price if self.entry_price > 0 else 0

    def current_value(self, current_price: float) -> float:
        return self.shares * current_price

    def unrealised_pnl(self, current_price: float) -> float:
        return self.current_value(current_price) - self.size_usdc


class PaperTrader:
    def __init__(self):
        self.balance: float = INITIAL_BALANCE
        self.positions: list[Position] = []
        self._pos_counter: int = 0
        self._load()

    # ------------------------------------------------------------------ #
    #  Public API
    # ------------------------------------------------------------------ #

    def open_position(
        self,
        market: PolyMarket,
        side: Side,
        signal: Signal,
        edge: float,
    ) -> Position | None:
        open_count = sum(1 for p in self.positions if p.state == "OPEN")
        if open_count >= MAX_OPEN_POSITIONS:
            log.info("Max open positions reached (%d)", MAX_OPEN_POSITIONS)
            return None
        if self.balance < BET_SIZE:
            log.info("Insufficient paper balance: $%.2f", self.balance)
            return None

        price = market.yes_price if side == "YES" else market.no_price
        if price <= 0 or price >= 1:
            log.warning("Bad price %.4f for %s", price, market.question[:40])
            return None

        self._pos_counter += 1
        pos = Position(
            pos_id       = f"P{self._pos_counter:04d}",
            market_id    = market.market_id,
            question     = market.question,
            side         = side,
            entry_price  = price,
            size_usdc    = BET_SIZE,
            news_headline = signal.headline,
            coin         = signal.coin,
            sentiment    = signal.sentiment,
        )
        self.balance -= BET_SIZE
        self.positions.append(pos)
        self._save()
        log.info("OPEN  %s | %s @ %.4f | edge=%.1f%% | %s",
                 pos.pos_id, side, price, edge * 100, market.question[:50])
        return pos

    def close_position(self, pos: Position, current_price: float) -> float:
        if pos.state != "OPEN":
            return 0.0
        pnl = pos.unrealised_pnl(current_price)
        received = pos.size_usdc + pnl
        self.balance += received
        pos.close_price = current_price
        pos.close_ts = time.time()
        pos.pnl = round(pnl, 4)
        if pnl > 0.01:
            pos.state = "CLOSED_WIN"
        elif pnl < -0.01:
            pos.state = "CLOSED_LOSS"
        else:
            pos.state = "CLOSED_NEUTRAL"
        self._save()
        log.info("CLOSE %s | pnl=%.4f | state=%s", pos.pos_id, pnl, pos.state)
        return pnl

    # ------------------------------------------------------------------ #
    #  Stats helpers
    # ------------------------------------------------------------------ #

    @property
    def open_positions(self) -> list[Position]:
        return [p for p in self.positions if p.state == "OPEN"]

    @property
    def closed_positions(self) -> list[Position]:
        return [p for p in self.positions if p.state != "OPEN"]

    @property
    def total_pnl(self) -> float:
        return round(sum(p.pnl for p in self.closed_positions), 4)

    @property
    def win_rate(self) -> float:
        closed = self.closed_positions
        if not closed:
            return 0.0
        wins = sum(1 for p in closed if p.state == "CLOSED_WIN")
        return round(wins / len(closed) * 100, 1)

    def stats_text(self) -> str:
        open_ps = self.open_positions
        closed = self.closed_positions
        lines = [
            f"💰 Balance: ${self.balance:.2f} USDC",
            f"📈 Total PnL: {'+' if self.total_pnl >= 0 else ''}${self.total_pnl:.4f}",
            f"🏆 Win rate: {self.win_rate}%  "
            f"({sum(1 for p in closed if p.state=='CLOSED_WIN')}W / "
            f"{sum(1 for p in closed if p.state=='CLOSED_LOSS')}L)",
            f"📂 Open positions: {len(open_ps)} / {MAX_OPEN_POSITIONS}",
            f"📊 Total trades: {len(self.positions)}",
        ]
        if open_ps:
            lines.append("")
            lines.append("🔓 <b>Open positions:</b>")
            for p in open_ps:
                lines.append(
                    f"  {p.pos_id} | {p.side} | ${p.size_usdc:.0f} @ {p.entry_price:.4f} | {p.question[:45]}"
                )
        return "\n".join(lines)

    def last_trades_text(self, n: int = 5) -> str:
        recent = sorted(self.closed_positions,
                        key=lambda p: p.close_ts, reverse=True)[:n]
        if not recent:
            return "Нет закрытых сделок."
        lines = [f"📋 <b>Последние {len(recent)} сделок:</b>"]
        for p in recent:
            icon = "✅" if p.state == "CLOSED_WIN" else "❌"
            lines.append(
                f"{icon} {p.pos_id} {p.side} | "
                f"{'+'if p.pnl>=0 else ''}${p.pnl:.4f} | "
                f"{p.question[:40]}"
            )
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    #  Persistence
    # ------------------------------------------------------------------ #

    def _save(self):
        try:
            data = {
                "balance": self.balance,
                "counter": self._pos_counter,
                "positions": [asdict(p) for p in self.positions],
            }
            SAVE_FILE.write_text(json.dumps(data, indent=2))
        except Exception as e:
            log.warning("Save failed: %s", e)

    def _load(self):
        if not SAVE_FILE.exists():
            return
        try:
            data = json.loads(SAVE_FILE.read_text())
            self.balance = data.get("balance", INITIAL_BALANCE)
            self._pos_counter = data.get("counter", 0)
            for pd in data.get("positions", []):
                self.positions.append(Position(**pd))
            log.info("Loaded %d positions from disk", len(self.positions))
        except Exception as e:
            log.warning("Load failed: %s", e)
