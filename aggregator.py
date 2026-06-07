import asyncio
import hashlib
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from dataclasses import dataclass, field
 
import aiohttp
 
from config import RSS_FEEDS, POLL_INTERVAL
 
log = logging.getLogger(__name__)
 
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; NewsArbBot/1.0)",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}
 
 
@dataclass
class NewsItem:
    headline: str
    source: str
    url: str
    pub_date: datetime
    uid: str = field(init=False)
 
    def __post_init__(self):
        raw = (self.headline + self.source).lower().encode()
        self.uid = hashlib.md5(raw).hexdigest()[:12]
 
 
def _parse_rss(xml_text: str, source_name: str) -> list[NewsItem]:
    items = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        log.warning("XML parse error for %s: %s", source_name, e)
        return items
 
    # handle both RSS <item> and Atom <entry>
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    entries = root.findall(".//item") or root.findall(".//atom:entry", ns)
 
    for entry in entries:
        def _text(tag, ns_tag=None):
            el = entry.find(tag)
            if el is None and ns_tag:
                el = entry.find(ns_tag, ns)
            return (el.text or "").strip() if el is not None else ""
 
        title = _text("title")
        link  = _text("link") or _text("atom:link", "atom:link")
        pub   = _text("pubDate") or _text("published") or _text("updated")
 
        if not title:
            continue
 
        try:
            # normalise various date formats
            for fmt in ("%a, %d %b %Y %H:%M:%S %z",
                        "%a, %d %b %Y %H:%M:%S %Z",
                        "%Y-%m-%dT%H:%M:%S%z",
                        "%Y-%m-%dT%H:%M:%SZ"):
                try:
                    dt = datetime.strptime(pub, fmt)
                    break
                except ValueError:
                    continue
            else:
                dt = datetime.now(timezone.utc)
        except Exception:
            dt = datetime.now(timezone.utc)
 
        items.append(NewsItem(title, source_name, link, dt))
 
    return items
 
 
SEEN_FILE = "seen_news.json"
 
 
class NewsAggregator:
    def __init__(self):
        self._seen: set[str] = self._load_seen()
        self._queue: asyncio.Queue = asyncio.Queue()
        self._session: aiohttp.ClientSession | None = None
 
    def _load_seen(self) -> set[str]:
        import json
        from pathlib import Path
        try:
            if Path(SEEN_FILE).exists():
                data = json.loads(Path(SEEN_FILE).read_text())
                seen = set(data.get("uids", []))
                log.info("Loaded %d seen UIDs from disk", len(seen))
                return seen
        except Exception as e:
            log.warning("Could not load seen file: %s", e)
        return set()
 
    def _save_seen(self):
        import json
        from pathlib import Path
        try:
            recent = list(self._seen)[-2000:]
            Path(SEEN_FILE).write_text(json.dumps({"uids": recent}))
        except Exception as e:
            log.warning("Could not save seen file: %s", e)
 
    async def start(self):
        connector = aiohttp.TCPConnector(ttl_dns_cache=300)
        self._session = aiohttp.ClientSession(
            connector=connector, headers=HEADERS
        )
        asyncio.create_task(self._poll_loop())
        log.info("Aggregator started — %d feeds", len(RSS_FEEDS))
 
    async def stop(self):
        if self._session:
            await self._session.close()
 
    async def get_news(self) -> NewsItem:
        """Blocks until a new (unseen) news item arrives."""
        return await self._queue.get()
 
    async def _poll_loop(self):
        """
        - первый прогон: запоминаем все текущие UIDs + заголовки, не торгуем
        - следующие прогоны: всё новое → в очередь
        Дедупликация работает и после редеплоя — храним заголовки в _seen.
        """
        first_run = True
 
        while True:
            tasks = [self._fetch_feed(f) for f in RSS_FEEDS]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            new_count = 0
 
            for res in results:
                if isinstance(res, Exception):
                    log.warning("Feed fetch error: %s", res)
                    continue
                for item in res:
                    # Дедупликация по UID И по заголовку (защита от редеплоев)
                    import hashlib
                    headline_uid = "h:" + hashlib.md5(item.headline.lower().encode()).hexdigest()[:12]
                    if item.uid in self._seen or headline_uid in self._seen:
                        continue
                    self._seen.add(item.uid)
                    self._seen.add(headline_uid)
                    if first_run:
                        continue  # первый прогон — только запоминаем
                    await self._queue.put(item)
                    new_count += 1
                    log.info("NEW  [%s] %s", item.source, item.headline[:70])
 
            if first_run:
                log.info("First run: marked %d headlines as seen", len(self._seen))
                first_run = False
            elif new_count > 0:
                log.info("Added %d new headlines to queue", new_count)
            else:
                log.debug("No new headlines this cycle (total seen: %d)", len(self._seen))
 
            # keep dedup set bounded
            if len(self._seen) > 10000:
                self._seen = set(list(self._seen)[-4000:])
 
            self._save_seen()
            await asyncio.sleep(POLL_INTERVAL)
 
    async def _fetch_feed(self, feed: dict) -> list[NewsItem]:
        url, name = feed["url"], feed["name"]
        try:
            async with self._session.get(
                url, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    log.warning("%s returned HTTP %d", name, resp.status)
                    return []
                text = await resp.text(errors="replace")
                return _parse_rss(text, name)
        except asyncio.TimeoutError:
            log.warning("Timeout fetching %s", name)
            return []
        except Exception as e:
            log.warning("Error fetching %s: %s", name, e)
            return []
