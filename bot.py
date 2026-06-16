import asyncio
import logging
import os
import time
from datetime import datetime, timezone

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message

from config import BOT_TOKEN, OWNER_ID
from aggregator import NewsAggregator
from nlp_engine import analyze
from market_scanner import MarketScanner
from signal_router import route
from paper_trader import PaperTrader
import api_server

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
#  Bot & dispatcher
# ------------------------------------------------------------------ #
bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()

# Global state
aggregator = NewsAggregator()
scanner    = MarketScanner()
trader     = PaperTrader()

running = False
_task: asyncio.Task | None = None
recent_news: list[dict] = []          # last 10 triggered news items


# ------------------------------------------------------------------ #
#  Owner-only guard
# ------------------------------------------------------------------ #
def owner_only(func):
    import functools
    @functools.wraps(func)
    async def wrapper(message: Message, **kwargs):
        if message.from_user.id != OWNER_ID:
            await message.answer("⛔ Access denied.")
            return
        return await func(message, **kwargs)
    return wrapper


# ------------------------------------------------------------------ #
#  Main trading loop
# ------------------------------------------------------------------ #
async def trading_loop():
    global running, recent_news
    log.info("Trading loop started")

    while running:
        try:
            news = await asyncio.wait_for(aggregator.get_news(), timeout=5.0)
        except asyncio.TimeoutError:
            await _monitor_open_positions()
            continue
        except Exception as e:
            log.warning("Queue error: %s", e)
            await asyncio.sleep(1)
            continue

        # --- NLP ---
        signal = analyze(news.headline, news.source)
        if signal is None:
            log.info("SKIP (no signal): %s", news.headline[:60])
            continue

        log.info("Signal: %s", signal)

        # --- find markets ---
        stale_markets = await scanner.find_stale_markets(signal.coin)
        if not stale_markets:
            all_markets = await scanner.find_markets(signal.coin)
            if not all_markets:
                log.info("SKIP (no markets for %s): %s", signal.coin, news.headline[:50])
                continue
            stale_markets = all_markets

        log.info("Found %d markets for %s", len(stale_markets), signal.coin)

        # --- route to trade ---
        decisions = route(signal, stale_markets)
        if not decisions:
            log.info("SKIP (no decisions after routing): %s", news.headline[:50])
            continue

        # --- execute best trade ---
        decision = decisions[0]
        pos = trader.open_position(
            decision.market, decision.side, signal, decision.edge
        )
        if pos is None:
            continue

        # --- remember for /news command ---
        recent_news.insert(0, {
            "headline": news.headline,
            "source":   news.source,
            "coin":     signal.coin,
            "sentiment": signal.sentiment,
            "side":     decision.side,
            "edge":     decision.edge,
            "question": decision.market.question,
            "pos_id":   pos.pos_id,
            "time":     datetime.now().strftime("%H:%M:%S"),
        })
        recent_news = recent_news[:10]

        # --- Telegram notification ---
        sign = "📈" if signal.sentiment > 0 else "📉"
        text = (
            f"{sign} <b>Новая сделка открыта</b>\n\n"
            f"📰 <b>Новость:</b> {news.headline}\n"
            f"🏦 <b>Источник:</b> {news.source}\n\n"
            f"🪙 <b>Монета:</b> {signal.coin}  "
            f"| Сентимент: {signal.sentiment:+.2f}\n"
            f"🎯 <b>Рынок:</b> {decision.market.question}\n"
            f"📊 <b>Сторона:</b> {decision.side} @ {pos.entry_price:.4f}\n"
            f"💵 <b>Ставка:</b> ${pos.size_usdc:.0f}  "
            f"| Edge: {decision.edge*100:.1f}%\n"
            f"⏱ MM lag: {decision.market.last_trade_age:.0f}s\n\n"
            f"🆔 {pos.pos_id}  |  💰 Баланс: ${trader.balance:.2f}"
        )
        await _notify(text)

    log.info("Trading loop stopped")


async def _check_5m_market_result(pos, session) -> tuple[bool, float, str]:
    """Проверяем разрешился ли 5-минутный рынок через Gamma API."""
    try:
        url = f"https://gamma-api.polymarket.com/markets/{pos.market_id}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status != 200:
                return False, pos.entry_price, ""
            data = await resp.json()

            # Рынок закрыт если closed=true или active=false
            closed = data.get("closed", False)
            active = data.get("active", True)
            if not closed and active:
                return False, pos.entry_price, ""

            # outcomePrices — список цен ["1", "0"] или ["0", "1"]
            # outcomes — список исходов ["Up", "Down"] или ["Yes", "No"]
            outcome_prices = data.get("outcomePrices", [])
            outcomes = data.get("outcomes", [])

            log.info("5m market closed: outcomes=%s prices=%s", outcomes, outcome_prices)

            if outcome_prices and outcomes:
                for i, outcome_name in enumerate(outcomes):
                    if i < len(outcome_prices):
                        price = float(outcome_prices[i])
                        # Сопоставляем с нашей стороной
                        # YES/UP → outcomes[0], NO/DOWN → outcomes[1]
                        our_side = pos.side.upper()
                        outcome_up = outcome_name.upper()
                        if our_side in (outcome_up, "YES" if outcome_up == "UP" else "UP" if outcome_up == "YES" else ""):
                            return True, price, outcome_name

                # Если не нашли по имени — берём по индексу
                # YES/UP — обычно первый токен (index 0)
                idx = 0 if pos.side.upper() in ("YES", "UP") else 1
                if idx < len(outcome_prices):
                    return True, float(outcome_prices[idx]), outcomes[idx] if idx < len(outcomes) else ""

            # Fallback: смотрим tokens
            tokens = data.get("tokens", [])
            for tok in tokens:
                tok_outcome = str(tok.get("outcome", "")).upper()
                our_side = pos.side.upper()
                if tok_outcome == our_side or (tok_outcome == "UP" and our_side == "YES") or (tok_outcome == "DOWN" and our_side == "NO"):
                    price = float(tok.get("price", pos.entry_price))
                    return True, price, tok_outcome

            return True, pos.entry_price, "unknown"
    except Exception as e:
        log.warning("5m result check error: %s", e)
        return False, pos.entry_price, ""


async def _monitor_open_positions():
    """Close positions: 5m markets by resolution, others by price/timeout."""
    if not trader.open_positions:
        return

    import time
    MAX_AGE = 15 * 60  # 15 минут максимум (5m рынок + буфер)

    async with aiohttp.ClientSession() as session:
        for pos in list(trader.open_positions):
            age_sec = time.time() - pos.open_ts
            is_5m = any(kw in pos.question.lower() for kw in ["up or down", "updown", "5m", "5:"])

            if is_5m:
                # Для 5m рынков — проверяем resolved статус
                resolved, exit_price, winner = await _check_5m_market_result(pos, session)

                if not resolved and age_sec < MAX_AGE:
                    continue  # ещё не разрешился, ждём

                if not resolved and age_sec >= MAX_AGE:
                    # Таймаут — закрываем по текущей цене
                    exit_price = pos.entry_price
                    winner = "timeout"

                pnl = trader.close_position(pos, exit_price)
                icon = "✅" if pnl > 0 else "❌"

                if winner == "timeout":
                    reason = "таймаут 15 мин"
                elif exit_price >= 0.9:
                    reason = "WIN ✨"
                else:
                    reason = "LOSS"

                await _notify(
                    f"{icon} <b>5m сделка закрыта</b> [{reason}]\n"
                    f"{'+'if pnl>=0 else ''}$<b>{abs(pnl):.4f}</b>\n"
                    f"{pos.question[:55]}\n"
                    f"Сторона: {pos.side} | Entry: {pos.entry_price:.4f} → Exit: {exit_price:.4f}\n"
                    f"💰 Баланс: ${trader.balance:.2f}"
                )
            else:
                # Для обычных рынков — по цене или таймауту 4ч
                current = pos.entry_price
                markets = await scanner.find_markets(pos.coin)
                target = next((m for m in markets if m.market_id == pos.market_id), None)
                if target is not None:
                    current = target.yes_price if pos.side == "YES" else target.no_price

                pnl_ratio = (current - pos.entry_price) / pos.entry_price
                if pos.side == "NO":
                    pnl_ratio = -pnl_ratio

                timeout = age_sec >= 4 * 3600
                should_close = pnl_ratio >= 0.03 or pnl_ratio <= -0.08 or timeout

                if should_close:
                    pnl = trader.close_position(pos, current)
                    icon = "✅" if pnl > 0 else "❌"
                    reason = "таймаут 4ч" if timeout else ("тейк +3%" if pnl > 0 else "стоп -8%")
                    await _notify(
                        f"{icon} <b>Сделка закрыта</b> [{reason}]  {pos.pos_id}\n"
                        f"{'+'if pnl>=0 else ''}$<b>{abs(pnl):.4f}</b>\n"
                        f"{pos.question[:50]}\n"
                        f"Entry: {pos.entry_price:.4f} → Exit: {current:.4f}\n"
                        f"💰 Баланс: ${trader.balance:.2f}"
                    )


async def _notify(text: str):
    if OWNER_ID:
        try:
            await bot.send_message(OWNER_ID, text, parse_mode="HTML")
        except Exception as e:
            log.warning("Notify failed: %s", e)


# ------------------------------------------------------------------ #
#  Telegram handlers
# ------------------------------------------------------------------ #
@dp.message(Command("start"))
@owner_only
async def cmd_start(message: Message):
    global running, _task
    if running:
        await message.answer("⚠️ Бот уже запущен.")
        return
    running = True
    api_server.set_running(True)
    _task = asyncio.create_task(trading_loop())
    await message.answer(
        "🚀 <b>PolyArb запущен</b>\n\n"
        "Paper trading активен. Слежу за RSS лентами:\n"
        "• CoinDesk\n• CoinTelegraph\n• Decrypt\n\n"
        "Команды: /status /news /trades /stop",
        parse_mode="HTML"
    )


@dp.message(Command("stop"))
@owner_only
async def cmd_stop(message: Message):
    global running, _task
    if not running:
        await message.answer("⚠️ Бот не запущен.")
        return
    running = False
    api_server.set_running(False)
    if _task:
        _task.cancel()
    await message.answer("⏹ Бот остановлен.")


@dp.message(Command("status"))
@owner_only
async def cmd_status(message: Message):
    status = "🟢 ACTIVE" if running else "🔴 STOPPED"
    text = f"{status}\n\n{trader.stats_text()}"
    await message.answer(text, parse_mode="HTML")


@dp.message(Command("news"))
@owner_only
async def cmd_news(message: Message):
    if not recent_news:
        await message.answer("📭 Новостей ещё не было.")
        return
    lines = ["📰 <b>Последние триггеры:</b>\n"]
    for n in recent_news[:5]:
        sign = "📈" if n["sentiment"] > 0 else "📉"
        lines.append(
            f"{sign} [{n['time']}] {n['source']}\n"
            f"  <i>{n['headline'][:80]}</i>\n"
            f"  {n['coin']} | {n['side']} | Edge {n['edge']*100:.1f}% | {n['pos_id']}\n"
        )
    await message.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("trades"))
@owner_only
async def cmd_trades(message: Message):
    await message.answer(trader.last_trades_text(7), parse_mode="HTML")


@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "📖 <b>Команды:</b>\n"
        "/start — запустить мониторинг\n"
        "/stop  — остановить\n"
        "/status — баланс и позиции\n"
        "/news   — последние новости-триггеры\n"
        "/trades — последние закрытые сделки",
        parse_mode="HTML"
    )


async def _fetch_btc_price_loop():
    """Фоновая задача — тянет цену BTC каждые 5 сек для графика.
    Пробует несколько источников: CoinGecko → Kraken → Bybit.
    """
    sources = [
        {
            "url": "https://api.kraken.com/0/public/Ticker?pair=XBTUSD",
            "parse": lambda d: float(d["result"]["XXBTZUSD"]["c"][0]),
        },
        {
            "url": "https://api.bybit.com/v5/market/tickers?category=spot&symbol=BTCUSDT",
            "parse": lambda d: float(d["result"]["list"][0]["lastPrice"]),
        },
        {
            "url": "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd",
            "parse": lambda d: float(d["bitcoin"]["usd"]),
        },
    ]
    async with aiohttp.ClientSession() as session:
        while True:
            for src in sources:
                try:
                    async with session.get(
                        src["url"], timeout=aiohttp.ClientTimeout(total=8)
                    ) as r:
                        if r.status == 200:
                            data = await r.json()
                            price = src["parse"](data)
                            if price and price > 0:
                                api_server.push_price(price)
                                break  # успех — не пробуем следующий
                except Exception:
                    continue  # пробуем следующий источник
            await asyncio.sleep(30)  # 30 сек — достаточно для графика, не бьём rate limit


# ------------------------------------------------------------------ #
#  Entry point
# ------------------------------------------------------------------ #
async def main():
    global running, _task
    api_server.init(trader, recent_news, {})
    await api_server.start(port=int(os.getenv("PORT", "8080")))
    await aggregator.start()
    await scanner.start()
    asyncio.create_task(_fetch_btc_price_loop())

    # Автостарт — запускаем торговлю сразу без ручного /start
    running = True
    api_server.set_running(True)
    _task = asyncio.create_task(trading_loop())
    log.info("Auto-started trading loop")

    log.info("Starting bot polling…")
    try:
        await dp.start_polling(bot)
    finally:
        await aggregator.stop()
        await scanner.stop()
        await api_server.stop()
        await bot.session.close()


if __name__ == "__main__":
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN env var not set")
    if not OWNER_ID:
        raise RuntimeError("OWNER_ID env var not set")
    asyncio.run(main())
