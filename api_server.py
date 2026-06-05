"""
api_server.py — лёгкий HTTP сервер внутри бота.
Отдаёт данные дашборду через GET /api/state
Запускается вместе с ботом в том же event loop.
"""
import asyncio
import json
import logging
import time
from aiohttp import web

log = logging.getLogger(__name__)

# Будет заполнено из bot.py
_trader     = None
_aggregator = None
_recent_news: list[dict] = []
_running = False
_start_ts: float = time.time()
_btc_prices: list[dict] = []   # {t, price} — последние 300 точек для графика


def init(trader, recent_news_ref: list, running_ref: dict):
    """Вызывается из bot.py при старте."""
    global _trader, _recent_news, _running
    _trader      = trader
    _recent_news = recent_news_ref


def push_price(price: float):
    """Вызывается из bot.py каждый тик цены BTC."""
    global _btc_prices
    _btc_prices.append({"t": int(time.time() * 1000), "p": round(price, 2)})
    if len(_btc_prices) > 500:
        _btc_prices = _btc_prices[-300:]


def set_running(state: bool):
    global _running
    _running = state


# ------------------------------------------------------------------ #
#  Handlers
# ------------------------------------------------------------------ #

async def handle_state(request: web.Request) -> web.Response:
    if _trader is None:
        return web.json_response({"error": "not ready"}, status=503)

    closed = _trader.closed_positions
    open_p = _trader.open_positions
    wins   = sum(1 for p in closed if p.state == "CLOSED_WIN")
    losses = sum(1 for p in closed if p.state == "CLOSED_LOSS")

    positions_out = []
    for p in _trader.positions[-50:]:   # последние 50
        positions_out.append({
            "id":          p.pos_id,
            "side":        p.side,
            "state":       p.state,
            "entry":       p.entry_price,
            "exit":        p.close_price,
            "size":        p.size_usdc,
            "pnl":         p.pnl,
            "coin":        p.coin,
            "question":    p.question,
            "headline":    p.news_headline,
            "sentiment":   p.sentiment,
            "open_ts":     int(p.open_ts * 1000),
            "close_ts":    int(p.close_ts * 1000) if p.close_ts else 0,
        })

    news_out = _recent_news[:10]

    data = {
        "running":       _running,
        "balance":       round(_trader.balance, 2),
        "initial":       1000.0,
        "total_pnl":     _trader.total_pnl,
        "daily_pnl":     _trader.total_pnl,   # упрощение для paper
        "win_rate":      _trader.win_rate,
        "trades_total":  len(_trader.positions),
        "wins":          wins,
        "losses":        losses,
        "open_count":    len(open_p),
        "positions":     positions_out,
        "recent_news":   news_out,
        "prices":        _btc_prices[-200:],
        "server_ts":     int(time.time() * 1000),
    }

    response = web.Response(
        text=json.dumps(data),
        content_type="application/json",
    )
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response


async def handle_health(request: web.Request) -> web.Response:
    return web.Response(text="ok")


# ------------------------------------------------------------------ #
#  Start / Stop
# ------------------------------------------------------------------ #

_runner: web.AppRunner | None = None

async def start(host: str = "0.0.0.0", port: int = 8080):
    global _runner
    app = web.Application()
    app.router.add_get("/api/state",  handle_state)
    app.router.add_get("/health",     handle_health)
    _runner = web.AppRunner(app)
    await _runner.setup()
    site = web.TCPSite(_runner, host, port)
    await site.start()
    log.info("API server listening on %s:%d", host, port)


async def stop():
    global _runner
    if _runner:
        await _runner.cleanup()
