"""
llm_analyzer.py — анализ новостей через Gemini Flash-Lite вместо ключевых слов.

Используем responseSchema — Gemini гарантированно возвращает валидный JSON
по заданной схеме, без markdown-обёрток и парсинга текста.

Если API недоступен (ошибка, rate limit, нет ключа) — возвращаем None,
и вызывающий код должен откатиться на keyword-based nlp_engine.analyze().
"""
import asyncio
import json
import logging
import os
import time

import aiohttp

log = logging.getLogger(__name__)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-3.1-flash-lite"
GEMINI_API_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)

SYSTEM_PROMPT = """Ты — аналитик крипто-новостей для алгоритмической торговли на 5-минутных рынках "BTC Up or Down" на Polymarket.

Твоя задача: оценить, в какую сторону вероятнее всего пойдёт цена BTC в ближайшие 5-15 минут после этой новости, и насколько рынок мог УЖЕ это учесть.

Правила:
- Конкретные факты (одобрение ETF, взлом, крупная покупка/продажа, регуляторное решение) — сильный сигнал, рынок может не успеть отреагировать.
- Аналитика, прогнозы, мнения экспертов ("could", "might", "analysts say") — слабый сигнал, рынок скорее всего уже это знает.
- Старые новости, дублирующиеся темы — игнорируй, direction="neutral".
- Учитывай реальный экономический смысл, а не только тон слов. "ETF outflow pain eases" — это позитивно, хотя слово "pain" звучит негативно.
- Geopolitical risk increasing → обычно негативно для BTC (risk-off). Risk decreasing/easing → обычно позитивно (risk-on).

confidence отражает: насколько сильный и неожиданный для рынка этот сигнал.
0.0-0.3 = шум/мнение, игнорировать. 0.3-0.6 = умеренный сигнал. 0.6-1.0 = сильный, конкретный, рынок вероятно не успел учесть.
reasoning — одно короткое предложение по-русски."""

# Gemini responseSchema — гарантирует структуру ответа без парсинга markdown
RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "direction": {
            "type": "STRING",
            "enum": ["up", "down", "neutral"],
        },
        "confidence": {
            "type": "NUMBER",
        },
        "reasoning": {
            "type": "STRING",
        },
    },
    "required": ["direction", "confidence", "reasoning"],
}


class LLMSignal:
    def __init__(self, direction: str, confidence: float, reasoning: str, headline: str):
        self.direction = direction       # "up" | "down" | "neutral"
        self.confidence = confidence     # 0.0 .. 1.0
        self.reasoning = reasoning
        self.headline = headline

    @property
    def sentiment(self) -> float:
        """Конвертируем в формат совместимый со старым Signal (-1.0 .. +1.0)."""
        if self.direction == "up":
            return self.confidence
        if self.direction == "down":
            return -self.confidence
        return 0.0

    @property
    def is_tradeable(self) -> bool:
        return self.direction != "neutral" and self.confidence >= 0.4

    def __str__(self):
        arrow = "📈" if self.direction == "up" else "📉" if self.direction == "down" else "➖"
        return f"{arrow} LLM conf={self.confidence:.2f} | {self.reasoning} | {self.headline[:50]}"


async def analyze_with_llm(
    headline: str,
    source: str,
    session: aiohttp.ClientSession,
    timeout_sec: float = 6.0,
) -> LLMSignal | None:
    """
    Анализирует заголовок через Gemini Flash-Lite.
    Возвращает None при любой ошибке — вызывающий код должен иметь fallback.
    """
    if not GEMINI_API_KEY:
        log.debug("GEMINI_API_KEY not set, skipping LLM analysis")
        return None

    headers = {
        "x-goog-api-key": GEMINI_API_KEY,
        "Content-Type": "application/json",
    }
    payload = {
        "system_instruction": {
            "parts": [{"text": SYSTEM_PROMPT}]
        },
        "contents": [
            {
                "role": "user",
                "parts": [{"text": f"Источник: {source}\nЗаголовок: {headline}"}],
            }
        ],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": RESPONSE_SCHEMA,
            "maxOutputTokens": 200,
            "temperature": 0.2,
        },
    }

    start = time.time()
    try:
        async with session.post(
            GEMINI_API_URL,
            headers=headers,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=timeout_sec),
        ) as resp:
            elapsed = time.time() - start
            if resp.status != 200:
                body = await resp.text()
                log.warning("Gemini API HTTP %d (%.1fs): %s", resp.status, elapsed, body[:200])
                return None

            data = await resp.json()

            candidates = data.get("candidates", [])
            if not candidates:
                log.warning("Gemini returned no candidates: %s", str(data)[:200])
                return None

            parts = candidates[0].get("content", {}).get("parts", [])
            text = "".join(p.get("text", "") for p in parts)

            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                log.warning("Gemini returned unparseable JSON: %s", text[:200])
                return None

            direction = str(parsed.get("direction", "neutral")).lower()
            if direction not in ("up", "down", "neutral"):
                direction = "neutral"
            confidence = float(parsed.get("confidence", 0.0))
            confidence = max(0.0, min(1.0, confidence))
            reasoning = str(parsed.get("reasoning", ""))[:200]

            log.info("Gemini (%.1fs): %s %.2f | %s", elapsed, direction, confidence, reasoning[:60])

            return LLMSignal(direction, confidence, reasoning, headline)

    except asyncio.TimeoutError:
        log.warning("Gemini API timeout after %.1fs", timeout_sec)
        return None
    except aiohttp.ServerTimeoutError:
        log.warning("Gemini API server timeout after %.1fs", timeout_sec)
        return None
    except Exception as e:
        log.warning("Gemini API error: %s", e)
        return None
