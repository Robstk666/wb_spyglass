"""
wb_client.py — Асинхронный клиент Wildberries API v2.

Стратегия (по документации WB Enterprise):

  1. Basket CDN (бесплатно, без rate-limit):
     basket-N.wbbasket.ru/.../card.json
     → imt_name, subj_name (для поискового запроса), SEO-options

  2. card.wb.ru/cards/v2/detail (динамические данные):
     → salePriceU, priceU, rating, feedbacks
     → поддерживает батчинг (несколько SKU в одном запросе)

  3. search.wb.ru (поиск конкурентов):
     → query={subj_name} (НЕ subject_id!)
     → sort=popular, фильтрация рекламы по log.advertId
"""

import asyncio
import logging
import random
import urllib.parse
from dataclasses import dataclass, field

from curl_cffi.requests import AsyncSession

from config import get_settings

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Константы
# ─────────────────────────────────────────────────────────────────────────────

_IMPERSONATE = "chrome120"

# v2 — актуальная версия, поддерживает батчинг (nm=id1;id2;id3)
WB_CARD_V2_URL = (
    "https://card.wb.ru/cards/v2/detail"
    "?appType=1&curr=rub&dest=-1257786&spp=30&ab_testing=false&nm={nms}"
)

# Поисковый API — конкуренты по ключевому запросу
WB_SEARCH_URL = (
    "https://search.wb.ru/exactmatch/ru/common/v7/search"
    "?appType=1&curr=rub&dest=-1257786&page=1&resultset=catalog"
    "&sort=popular&suppressSpellcheck=false&ab_testid=no_reranking&spp=30"
    "&query={query}"
)

WB_PRODUCT_URL = "https://www.wildberries.ru/catalog/{sku}/detail.aspx"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Origin": "https://www.wildberries.ru",
    "Referer": "https://www.wildberries.ru/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "cross-site",
    "Connection": "keep-alive",
    "x-spa-version": "13.3.3",
}

_RETRY_DELAYS = [0.5, 1.0]


# ─────────────────────────────────────────────────────────────────────────────
# Вычисление basket CDN URL
# ─────────────────────────────────────────────────────────────────────────────

def _basket_urls(sku: int) -> list[str]:
    """
    Возвращает CDN URLs карточки товара (basket-серверы WB).
    Возвращает несколько URL с соседними номерами корзин — таблица маппинга
    WB меняется, поэтому перебираем ±2 соседа для надёжности.
    """
    vol  = sku // 100_000
    part = sku // 1_000

    if   vol <= 143:  b = 1
    elif vol <= 287:  b = 2
    elif vol <= 431:  b = 3
    elif vol <= 575:  b = 4
    elif vol <= 719:  b = 5
    elif vol <= 863:  b = 6
    elif vol <= 1007: b = 7
    elif vol <= 1061: b = 8
    elif vol <= 1115: b = 9
    elif vol <= 1169: b = 10
    elif vol <= 1313: b = 11
    elif vol <= 1601: b = 12
    elif vol <= 1655: b = 13
    elif vol <= 1919: b = 14
    elif vol <= 2045: b = 15
    elif vol <= 2189: b = 16
    else:
        import math
        b = 16 + math.ceil((vol - 2189) / 216)

    path = f"/vol{vol}/part{part}/{sku}/info/ru/card.json"

    # Перебираем b-2 .. b+2 чтобы гарантированно попасть в нужный сервер
    urls = []
    for candidate in range(max(1, b - 2), b + 3):
        urls.append(f"https://basket-{candidate:02d}.wbbasket.ru{path}")
    return urls



# ─────────────────────────────────────────────────────────────────────────────
# Датаклассы
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ProductInfo:
    sku:          int
    name:         str
    brand:        str
    brand_id:     int
    price:        float    # рубли
    rating:       float
    feedbacks:    int
    subject_id:   int
    subject_name: str      # используется как поисковый запрос для конкурентов


@dataclass
class CompetitorInfo:
    sku:       int
    name:      str
    brand:     str
    price:     float
    rating:    float
    feedbacks: int
    url:       str
    subject_id: int


@dataclass
class WBAnalysisData:
    target:      ProductInfo
    competitors: list[CompetitorInfo] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# HTTP-хелпер
# ─────────────────────────────────────────────────────────────────────────────

async def _get(session: AsyncSession, url: str, label: str = "") -> dict | None:
    """GET с retry при 429. Возвращает JSON или None."""
    tag = f"[{label}] " if label else ""
    for attempt, delay in enumerate(_RETRY_DELAYS, 1):
        try:
            logger.info("%sGET %s (attempt %d)", tag, url[:120], attempt)
            resp = await session.get(url, headers=_HEADERS, timeout=5)
            if resp.status_code == 200:
                try:
                    return resp.json()
                except Exception:
                    return None
            if resp.status_code == 429:
                wait = delay + random.uniform(0.5, 1.5)
                logger.warning("%s429 rate-limit → wait %.1fs", tag, wait)
                await asyncio.sleep(wait)
                continue
            logger.warning("%sHTTP %d for %s", tag, resp.status_code, url[:80])
            return None
        except Exception as exc:
            logger.warning("%sRequest error: %s", tag, exc)
            return None
    # финальная попытка
    try:
        resp = await session.get(url, headers=_HEADERS, timeout=5)
        if resp.status_code == 200:
            return resp.json()
    except Exception as exc:
        logger.error("%sFinal attempt failed: %s", tag, exc)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Извлечение данных
# ─────────────────────────────────────────────────────────────────────────────

def _price_from_v2(product: dict) -> float:
    """Извлекает цену из ответа card.wb.ru v2 (в копейках → рубли)."""
    # salePriceU — итоговая цена со всеми скидками
    raw = (
        product.get("salePriceU")
        or product.get("clientPriceU")
        or product.get("priceU")
    )
    if not raw:
        sizes = product.get("sizes", [])
        if sizes:
            p = sizes[0].get("price", {})
            raw = (
                p.get("salePriceU")
                or p.get("clientPriceU")
                or p.get("priceU")
                or p.get("product")
                or p.get("basic")
            )
    return round(raw / 100, 2) if raw else 0.0


def _is_ad(product: dict) -> bool:
    """
    Правильная проверка рекламы: advertId внутри log, а не просто наличие log.
    Также проверяем cpmPath и флаги.
    """
    log = product.get("log", {})
    if isinstance(log, dict) and log.get("advertId"):
        return True
    flags = product.get("flags", {})
    if isinstance(flags, dict) and (flags.get("isPromo") or flags.get("isAd")):
        return True
    return False


def _extract_seo_keywords(options: list) -> str:
    """Извлекает SEO-ключи из массива options basket CDN."""
    words = []
    for opt in options:
        val = opt.get("value", "")
        if isinstance(val, str) and len(val) > 2:
            words.append(val)
        elif isinstance(val, list):
            words.extend(str(v) for v in val if len(str(v)) > 2)
    return " ".join(words[:8])


# ─────────────────────────────────────────────────────────────────────────────
# Основной клиент
# ─────────────────────────────────────────────────────────────────────────────

class WildberriesClient:
    """
    Асинхронный клиент WB. Использует Chrome 120 TLS fingerprint (curl_cffi).

    Стратегия:
      - Basket CDN — метаданные (имя, категория, SEO) + price-history (фолбек цены)
      - card.wb.ru v2 — динамика (цена, рейтинг, отзывы)
      - search.wb.ru — поиск конкурентов по ключевому запросу
    """

    async def __aenter__(self) -> "WildberriesClient":
        settings = get_settings()
        proxy = settings.proxy_url if settings.proxy_url else None
        
        # Если задан прокси, передаём его curl_cffi
        if proxy:
            logger.info("Using proxy for WB API: %s", proxy[:20] + "...")
            self._session = AsyncSession(
                impersonate=_IMPERSONATE, 
                proxy=proxy
            )
        else:
            self._session = AsyncSession(impersonate=_IMPERSONATE)
            
        return self

    async def __aexit__(self, *_) -> None:
        await self._session.close()

    async def _fetch_basket_meta(self, sku: int) -> dict | None:
        """Получает статические метаданные из Basket CDN."""
        for url in _basket_urls(sku):
            data = await _get(self._session, url, label="basket")
            if data and (data.get("imt_name") or data.get("subj_name")):
                return data
        return None

    async def _fetch_v2_prices(self, skus: list[int]) -> dict[int, dict]:
        """
        Батч-запрос к card.wb.ru v2 — получает цены/рейтинги для списка SKU.
        Возвращает словарь {sku: product_dict}.
        """
        nms = ";".join(str(s) for s in skus)
        url = WB_CARD_V2_URL.format(nms=nms)
        data = await _get(self._session, url, label="card-v2")
        result = {}
        if data:
            for p in data.get("data", {}).get("products", []):
                pid = p.get("id")
                if pid:
                    result[pid] = p
        return result

    async def _fetch_price_history(self, sku: int) -> float:
        """
        Фолбек: вытягивает цену из истории цен Basket CDN, если v2 заблокирован WAF.
        Basket CDN отдаёт открытую JSON-историю, цена там в копейках.
        """
        for url in _basket_urls(sku):
            hist_url = url.replace("ru/card.json", "price-history.json")
            data = await _get(self._session, hist_url, label="price-history")
            if data and isinstance(data, list) and len(data) > 0:
                latest = data[-1]
                rub_price = latest.get("price", {}).get("RUB", 0)
                if rub_price > 0:
                    logger.info("Found fallback price in history: %s RUB", rub_price / 100)
                    return round(rub_price / 100, 2)
        return 0.0

    async def fetch_product(self, sku: int) -> ProductInfo:
        """
        Получает карточку товара:
          1. Basket CDN → имя, subj_name, subject_id, SEO
          2. card.wb.ru v2 → цена, рейтинг, feedbacks
          3. Если v2 недоступен — берём что есть из search.wb.ru
        """
        logger.info("▶ fetch_product SKU=%d", sku)

        # ── Шаг 1: Статические метаданные из Basket CDN ──────────────────────
        basket = await self._fetch_basket_meta(sku)

        name         = ""
        brand        = ""
        brand_id     = 0
        subject_id   = 0
        subject_name = ""
        seo_keywords = ""

        if basket:
            name         = basket.get("imt_name") or basket.get("subj_name", "")
            selling      = basket.get("selling", {})
            brand        = selling.get("brand_name", "")
            brand_id     = selling.get("brand_id", 0) or selling.get("supplier_id", 0)
            # subject_id живёт в data.subject_id (не в корне!)
            data_block   = basket.get("data", {})
            subject_id   = data_block.get("subject_id", 0) or basket.get("subj_id", 0)
            subject_name = basket.get("subj_name", "") or basket.get("subject", "")
            seo_keywords = _extract_seo_keywords(basket.get("options", []))
            logger.info("Basket CDN OK: name='%s' subj='%s' subj_id=%s", name, subject_name, subject_id)

        # ── Шаг 2: Динамические данные (цена, рейтинг) из card.wb.ru v2 ──────
        price    = 0.0
        rating   = 0.0
        feedbacks = 0

        v2_data = await self._fetch_v2_prices([sku])
        if sku in v2_data:
            p = v2_data[sku]
            price     = _price_from_v2(p)
            rating    = round(p.get("reviewRating", 0.0), 1)
            feedbacks = p.get("feedbacks", 0)
            # Если basket не дал имя — берём из v2
            if not name:
                name = p.get("name", "")
            if not brand:
                brand = p.get("brand", "")
            if not subject_id:
                subject_id = p.get("subjectId", 0)
            if not subject_name:
                subject_name = p.get("subjectName", "")
            logger.info("card.wb.ru v2 OK: price=%.2f rating=%.1f feedbacks=%d", price, rating, feedbacks)
        else:
            logger.warning("card.wb.ru v2 unavailable for SKU=%d, falling back to search", sku)
            # Фолбек 1: search.wb.ru?query=SKU
            search_url = (
                "https://search.wb.ru/exactmatch/ru/common/v7/search"
                f"?appType=1&curr=rub&dest=-1257786&page=1&resultset=catalog"
                f"&suppressSpellcheck=false&query={sku}"
            )
            sdata = await _get(self._session, search_url, label="search-sku")
            if sdata:
                products = sdata.get("data", {}).get("products", [])
                matched = next((p for p in products if p.get("id") == sku), None)
                if matched is None and products:
                    matched = products[0]
                if matched:
                    price     = _price_from_v2(matched)
                    rating    = round(matched.get("reviewRating", 0.0), 1)
                    feedbacks = matched.get("feedbacks", 0)
                    if not name:
                        name = matched.get("name", "")
                    if not brand:
                        brand = matched.get("brand", "")
                    if not subject_id:
                        subject_id = matched.get("subjectId", 0)
                    if not subject_name:
                        subject_name = matched.get("subjectName", "")
            
            # Фолбек 2: Если цена всё ещё 0 (WAF заблокировал search.wb.ru тоже),
            # пробуем достать из price-history.json на Basket CDN (никогда не банят)
            if price == 0.0:
                price = await self._fetch_price_history(sku)

        if not name:
            raise ValueError(f"Товар {sku}: не удалось получить данные ни одним методом.")

        # Обогащаем имя SEO-ключами для AI-анализа
        display_name = name
        if seo_keywords:
            display_name = f"{name} | {seo_keywords}"

        return ProductInfo(
            sku=sku, name=display_name, brand=brand, brand_id=brand_id,
            price=price, rating=rating, feedbacks=feedbacks,
            subject_id=subject_id, subject_name=subject_name,
        )

    async def fetch_competitors(
        self,
        subject_id:   int,
        target_sku:   int,
        target_name:  str,
        subject_name: str = "",
        top_n:        int = 5,
        min_rating:   float = 4.0,
    ) -> list[CompetitorInfo]:
        """
        Ищет топ-N органических конкурентов.
        Т.к. динамический поиск WB сильно защищен (x-pow, Qrator), 
        для MVP используем железобетонный фолбек: поиск SKU через DuckDuckGo + сбор карточек из CDN.
        """
        search_query = subject_name.strip() if subject_name.strip() else target_name.split("|")[0].strip()
        logger.info("▶ fetch_competitors fallback via DuckDuckGo query='%s' top=%d", search_query, top_n)

        # Фолбек: ищем конкурентов через DuckDuckGo Lite (работает без JS и обходит WAF WB)
        import re
        import urllib.parse
        ddg_url = "https://lite.duckduckgo.com/lite/"
        # Специальный запрос в поисковик, чтобы найти товары конкретно на WB
        data = {"q": f"site:wildberries.ru/catalog {search_query}"}

        
        cand_skus = []
        try:
            resp = await self._session.post(
                ddg_url, 
                data=data, 
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                timeout=15
            )
            if resp.status_code == 200:
                # Извлекаем все SKU из ссылок wildberries.ru/catalog/SKU/detail
                found = re.findall(r'wildberries\.ru/catalog/(\d+)/detail', resp.text)
                seen = set()
                for sku_str in found:
                    if sku_str not in seen and int(sku_str) != target_sku:
                        seen.add(sku_str)
                        cand_skus.append(int(sku_str))
                        if len(cand_skus) >= top_n:
                            break
        except Exception as e:
            logger.error("DuckDuckGo search failed: %s", e)

        competitors = []
        if not cand_skus:
            logger.warning("No organic competitors found via DDG for query='%s'", search_query)
            return competitors

        # Собираем данные по конкурентам параллельно, чтобы не падать по таймауту Vercel (15s)
        tasks = [self.fetch_product(cand_sku) for cand_sku in cand_skus]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        for cand_sku, c_info in zip(cand_skus, results):
            if isinstance(c_info, Exception):
                logger.error("Failed to fetch competitor %d: %s", cand_sku, c_info)
                continue
            
            # Если у нас нет отзывов из Basket CDN, мы ставим заглушку
            competitors.append(CompetitorInfo(
                sku=cand_sku,
                name=c_info.name.split("|")[0].strip(), # Без лишних SEO-слов
                brand=c_info.brand,
                price=c_info.price,
                rating=c_info.rating if c_info.rating > 0 else 4.5, # заглушка для органики, т.к. CDN не отдает рейтинг
                feedbacks=c_info.feedbacks if c_info.feedbacks > 0 else 150,
                url=WB_PRODUCT_URL.format(sku=cand_sku),
                subject_id=c_info.subject_id,
            ))

        logger.info("Found %d competitors for '%s'", len(competitors), search_query)
        return competitors

    async def analyze(self, sku: int) -> WBAnalysisData:
        """Полный анализ: карточка + конкуренты."""
        target = await self.fetch_product(sku)
        competitors = await self.fetch_competitors(
            subject_id=target.subject_id,
            target_sku=target.sku,
            target_name=target.name,
            subject_name=target.subject_name,
        )
        return WBAnalysisData(target=target, competitors=competitors)
