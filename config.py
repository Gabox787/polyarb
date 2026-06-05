import os
 
# Telegram
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
 
# RSS источники
RSS_FEEDS = [
    {"name": "CoinDesk",      "url": "https://feeds.feedburner.com/CoinDesk"},
    {"name": "CoinTelegraph", "url": "https://cointelegraph.com/rss"},
    {"name": "Decrypt",       "url": "https://decrypt.co/feed"},
]
 
# Интервал опроса RSS (секунды)
POLL_INTERVAL = 30
 
# Paper trading
INITIAL_BALANCE = 1000.0   # виртуальных USDC
BET_SIZE = 20.0            # ставка на сделку
MIN_EDGE = 0.05            # минимальное ожидаемое преимущество (5%)
MAX_OPEN_POSITIONS = 5
 
# NLP — ключевые слова (без тяжёлых зависимостей)
BULLISH_KEYWORDS = [
    "surge", "rally", "bullish", "soar", "pump", "moon", "breakout",
    "all-time high", "ath", "adoption", "institutional", "etf approved",
    "bitcoin etf", "upgrade", "mainnet", "partnership", "buy",
    "record", "rise", "gain", "jump", "approve", "launch",
    "рост", "вырос", "одобр", "рекорд",
]
 
BEARISH_KEYWORDS = [
    "crash", "dump", "bearish", "plunge", "fall", "ban", "hack",
    "exploit", "bankruptcy", "sec", "lawsuit", "fraud", "sell",
    "collapse", "fear", "drop", "decline", "warning", "risk",
    "sanction", "restrict", "blocked", "crackdown",
    "падение", "запрет", "взлом", "мошен",
]
 
# Монеты → маркеры для поиска рынков
COIN_KEYWORDS = {
    "BTC":  ["bitcoin", "btc", "satoshi", "saylor", "strategy", "mstr",
              "blackrock", "grayscale", "microstrategy"],
    "ETH":  ["ethereum", "eth", "ether", "vitalik", "eip", "shapella"],
    "SOL":  ["solana", "sol", "solana's"],
    "BNB":  ["binance", "bnb", "cz "],
    "XRP":  ["xrp", "ripple"],
    "ZEC":  ["zcash", "zec", "z-cash"],
    "HYPE": ["hyperliquid", "hype"],
    "ADA":  ["cardano", "ada", "hoskinson"],
}
 
# Polymarket API (только чтение — paper trading)
POLYMARKET_GAMMA_API = "https://gamma-api.polymarket.com"
POLYMARKET_CLOB_API  = "https://clob.polymarket.com"
