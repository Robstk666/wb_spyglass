"""
wb_client.py — Асинхронный клиент Wildberries API с обходом WAF.

Проблема: WB WAF (Firewall) блокирует запросы от стандартных HTTP-библиотек
(httpx, requests), анализируя TLS-fingerprint (JA3/JA3S).

Решение: curl_cffi — обёртка над libcurl с поддержкой TLS-impersonation.
Параметр impersonate="chrome120" заставляет клиент отправлять точный
TLS ClientHello от Chrome 120, неотличимый от реального браузера.

Стратегия получения карточки товара:
  1. basket-N.wb.ru (CDN, без rate-limit, вычисляется из артикула)
  2. basket-N.wbbasket.com (резервный CDN домен)
  3. search.wb.ru ?query={sku} (fallback с retry при 429)

Стратегия получения конкурентов:
  search.wb.ru ?subject={subject_id} с retry при 429.
"""

import asyncio
import logging
import random
from dataclasses import dataclass, field

from curl_cffi.requests import AsyncSession

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Константы
# ─────────────────────────────────────────────────────────────────────────────

# Браузер для TLS-impersonation (Chrome 120 — актуальный, хорошо проходит WAF)
_IMPERSONATE = "chrome120"

# URL для поиска конкурентов (search.wb.ru)
WB_SEARCH_URL = (
    "https://search.wb.ru/exactmatch/ru/common/v7/search"
    "?appType=1&curr=rub&dest=-1257786&page=1&resultset=catalog"
    "&sort=popular&suppressSpellcheck=false&subject={subject_id}"
)

# URL для получения детальной карточки товара
WB_DETAIL_URL = "https://card.wb.ru/cards/v1/detail?appType=1&curr=rub&dest=-1257786&spp=30&nm={sku}"

# Поиск одного товара по артикулу (fallback для карточки)
WB_SEARCH_SINGLE_URL = (
    "https://search.wb.ru/exactmatch/ru/common/v7/search"
    "?appType=1&curr=rub&dest=-1257786&page=1&resultset=catalog"
    "&suppressSpellcheck=false&query={sku}"
)

# Публичная ссылка на карточку товара
WB_PRODUCT_URL = "https://www.wildberries.ru/catalog/{sku}/detail.aspx"

# Полный набор "человеческих" заголовков от десктопного Chrome
# Sec-Fetch-* критически важны — WAF их проверяет
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

# Задержки retry при 429 (секунды) + случайный jitter ±1s
_RETRY_DELAYS = [5, 15, 30]


# ─────────────────────────────────────────────────────────────────────────────
# Вычисление basket CDN URL
# ─────────────────────────────────────────────────────────────────────────────

def _get_basket_card_urls(sku: int) -> list[str]:
    """
    Возвращает список CDN URL карточки товара (basket-серверы WB).

    Алгоритм:
      vol  = sku // 100_000  — определяет номер сервера
      part = sku // 1_000    — директория

    Возвращает два домена (wb.ru основной, wbbasket.com резервный).
    Basket CDN отдаёт статичный JSON без rate-limit.
    """
    vol  = sku // 100_000
    part = sku // 1_000

    if   vol <= 143:  b_num = 1
    elif vol <= 287:  b_num = 2
    elif vol <= 431:  b_num = 3
    elif vol <= 719:  b_num = 4
    elif vol <= 1007: b_num = 5
    elif vol <= 1061: b_num = 6
    elif vol <= 1115: b_num = 7
    elif vol <= 1169: b_num = 8
    elif vol <= 1313: b_num = 9
    elif vol <= 1601: b_num = 10
    elif vol <= 1655: b_num = 11
    elif vol <= 1919: b_num = 12
    elif vol <= 2045: b_num = 13
    elif vol <= 2189: b_num = 14
    else:
        import math
        b_num = 14 + math.ceil((vol - 2189) / 216)

    path = f"/vol{vol}/part{part}/{sku}/info/ru/card.json"
    
    urls = []
    # Для новых артикулов WB часто смещает корзину на +-1, поэтому перебираем соседей:
    targets = [b_num] if b_num <= 14 else [b_num, b_num - 1, b_num + 1]
    
    for t in targets:
        s_num = f"{t:02d}"
        urls.append(f"https://basket-{s_num}.wbbasket.ru{path}")
        urls.append(f"https://basket-{s_num}.wb.ru{path}")
        
    return urls


# ─────────────────────────────────────────────────────────────────────────────
# Датаклассы — строгие контракты данных
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ProductInfo:
    """Данные целевого товара."""
    sku:          int
    name:         str
    brand:        str
    brand_id:     int
    price:        float    # в рублях
    rating:       float
    feedbacks:    int
    subject_id:   int
    subject_name: str


@dataclass
class CompetitorInfo:
    """Один конкурент из топа категории."""
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
    """Итоговый результат сбора данных."""
    target:      ProductInfo
    competitors: list[CompetitorInfo] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Вспомогательные функции
# ─────────────────────────────────────────────────────────────────────────────

def _safe_price(raw: int | None) -> float:
    """Конвертирует WB-цену (в копейках × 10) → рубли."""
    return round(raw / 100, 2) if raw else 0.0


def _extract_search_product(data: dict) -> dict:
    """
    Извлекает поля из одного объекта в data.products[]
    (формат search.wb.ru).
    """
    sizes = data.get("sizes", [])
    raw_price = 0
    # Приоритетное безопасное извлечение цены (сначала ищем в корне)
    raw_price = (
        data.get("clientPriceU") 
        or data.get("salePriceU") 
        or data.get("priceU")
    )
    # Если в корне нет, ищем внутри блока sizes[0].price (как часто бывает в новом API)
    if not raw_price and sizes:
        price_block = sizes[0].get("price", {})
        raw_price = (
            price_block.get("clientPriceU") 
            or price_block.get("salePriceU") 
            or price_block.get("priceU")
            or price_block.get("product") 
            or price_block.get("basic") 
            or 0
        )

    return {
        "sku":          data.get("id", 0),
        "name":         data.get("name", "").strip(),
        "brand":        data.get("brand", "").strip(),
        "brand_id":     data.get("brandId", 0),
        "price":        _safe_price(raw_price),
        "rating":       round(data.get("reviewRating", 0.0), 1),
        "feedbacks":    data.get("feedbacks", 0),
        "subject_id":   data.get("subjectId", 0),
        "subject_name": data.get("subjectName", ""),
    }


def _parse_basket_card(sku: int, card: dict) -> ProductInfo:
    """
    Парсит ответ basket CDN (card.json) в ProductInfo.

    Формат basket отличается от search API — плоская структура:
      imt_name  → название
      selling.* → цена и бренд
      review_rating → рейтинг
    """
    name     = card.get("imt_name") or card.get("subj_name", "")
    selling  = card.get("selling", {})
    brand    = selling.get("brand_name") or card.get("brand", "")
    brand_id = selling.get("brand_id") or 0

    # В basket CDN формат цены может отличаться, но также применяем безопасное извлечение (client_price, price_sale, price, imt_price)
    raw_price = (
        selling.get("client_price")
        or selling.get("price_sale")
        or selling.get("price")
        or card.get("imt_price", 0)
    )
    price        = _safe_price(raw_price)
    rating       = round(card.get("review_rating", 0.0), 1)
    feedbacks    = card.get("feedbacks", 0)
    subject_id   = card.get("subj_id", 0) or card.get("subject_id", 0)
    subject_name = card.get("subj_name", "") or card.get("subject", "")

    # Извлекаем SEO-слова (LSI-ядро) из массива options (характеристики товара)
    options = card.get("options", [])
    seo_words = []
    for opt in options:
        val = opt.get("value", "")
        if isinstance(val, str) and len(val) > 2:
            seo_words.append(val)
        elif isinstance(val, list):
            seo_words.extend([str(v) for v in val if len(str(v)) > 2])

    if seo_words:
        # Добавляем SEO слова в name, чтобы AI Analyzer мог найти их как "упущенные ключи"
        # Делаем это аккуратно, чтобы не испортить внешний вид (добавляем через разделитель)
        # Так как фронт и бэк не ждут нового поля, это самый безопасный способ передачи LSI.
        seo_text = " ".join(seo_words[:5]) # берем топ-5 слов
        name = f"{name} ({seo_text})"

    logger.info(
        "Basket CDN parsed: name='%s' brand='%s' price=%.2f subjectId=%s",
        name, brand, price, subject_id,
    )
    return ProductInfo(
        sku=sku, name=name, brand=brand, brand_id=brand_id,
        price=price, rating=rating, feedbacks=feedbacks,
        subject_id=subject_id, subject_name=subject_name,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Низкоуровневый HTTP-хелпер с retry
# ─────────────────────────────────────────────────────────────────────────────

async def _get_with_retry(
    session: AsyncSession,
    url: str,
    label: str = "",
) -> dict | None:
    """
    Выполняет GET-запрос через curl_cffi. Если запрос к WB API, он обернут 
    через AllOrigins прокси.


    - При 200 → возвращает распарсенный JSON.
    - При 429 → retry с задержками 5s / 15s / 30s + jitter.
    - При 4xx (кроме 429) → возвращает None (не паникуем, пробуем дальше).
    - При сетевой ошибке → возвращает None.

    Args:
        session: открытая curl_cffi AsyncSession
        url:     целевой URL
        label:   описание для логов

    Returns:
        dict с JSON-ответом или None при неудаче.
    """
    tag = f"[{label}] " if label else ""

    for attempt, delay in enumerate(_RETRY_DELAYS, start=1):
        try:
            logger.info("%sGET %s (attempt %d)", tag, url, attempt)
            resp = await session.get(url, headers=_HEADERS, timeout=15)

            if resp.status_code == 200:
                return resp.json()

            if resp.status_code == 429:
                jitter = random.uniform(0.5, 1.5)
                wait   = delay + jitter
                logger.warning(
                    "%s429 rate-limit. Waiting %.1fs before retry %d/%d",
                    tag, wait, attempt, len(_RETRY_DELAYS),
                )
                await asyncio.sleep(wait)
                continue

            # 404, 403, 5xx — не retry, просто сообщаем
            logger.warning("%sHTTP %s for %s", tag, resp.status_code, url)
            return None

        except Exception as exc:
            logger.warning("%sRequest error: %s — %s", tag, url, exc)
            return None

    # Финальная попытка после последней задержки
    try:
        logger.info("%sFinal attempt: GET %s", tag, url)
        resp = await session.get(url, headers=_HEADERS, timeout=15)
        if resp.status_code == 200:
            return resp.json()
        logger.error("%sFailed after all retries. Last status: %s", tag, resp.status_code)
    except Exception as exc:
        logger.error("%sFinal attempt failed: %s", tag, exc)

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Основной клиент
# ─────────────────────────────────────────────────────────────────────────────

class WildberriesClient:
    """
    Асинхронный клиент Wildberries с TLS-impersonation (curl_cffi).

    Использует Chrome 120 TLS fingerprint для обхода WAF WB.
    Создаётся через async context manager:

        async with WildberriesClient() as wb:
            data = await wb.analyze(sku)
    """

    async def __aenter__(self) -> "WildberriesClient":
        # Одна сессия на весь запрос — keep-alive соединения
        self._session = AsyncSession(impersonate=_IMPERSONATE)
        return self

    async def __aexit__(self, *_) -> None:
        await self._session.close()

    # ── Получение карточки товара ─────────────────────────────────────────────

    async def fetch_product(self, sku: int) -> ProductInfo:
        """
        Получает данные карточки товара по SKU.

        Стратегия (без моков — только реальные данные):
          1. search.wb.ru ?query=  → дает полные данные (цена, рейтинг, subject_id)
          2. basket-N.wb.ru    → статичный CDN (fallback без цены и subject_id)

        Raises:
            ValueError: товар не найден ни одним методом.
        """
        logger.info("▶ fetch_product SKU=%d", sku)
        
        # ── 1: search.wb.ru (Основной API, дает полные данные) ───
        search_url = WB_SEARCH_SINGLE_URL.format(sku=sku)
        data = await _get_with_retry(self._session, search_url, label="search-single")

        if data:
            products = data.get("data", {}).get("products", [])
            # Ищем точное совпадение по ID, иначе берём первый результат
            matched = next((p for p in products if p.get("id") == sku), None)
            if matched is None and products:
                matched = products[0]
            if matched is not None:
                fields = _extract_search_product(matched)
                return ProductInfo(
                    sku=fields["sku"],         name=fields["name"],
                    brand=fields["brand"],     brand_id=fields["brand_id"],
                    price=fields["price"],     rating=fields["rating"],
                    feedbacks=fields["feedbacks"],
                    subject_id=fields["subject_id"],
                    subject_name=fields["subject_name"],
                )

        logger.info("Search API unavailable/incomplete for SKU=%d, trying Basket CDN", sku)

        # ── 3: Basket CDN (Absolute Fallback, нет цены и subject_id) ──────────────
        for basket_url in _get_basket_card_urls(sku):
            data = await _get_with_retry(self._session, basket_url, label="basket")
            if data and data.get("imt_name"):
                logger.info("Product found via Basket CDN: %s", data.get("imt_name"))
                return _parse_basket_card(sku, data)

        raise ValueError(
            f"Товар с артикулом {sku} недоступен: "
            f"все методы (detail, search, basket CDN) завершились неудачей."
        )

    # ── Поиск конкурентов ─────────────────────────────────────────────────────

    async def fetch_competitors(
        self,
        subject_id:  int,
        target_sku:  int,
        target_name: str,
        top_n:       int   = 5,
        min_rating:  float = 4.5,
    ) -> list[CompetitorInfo]:
        """
        Ищет топ-N конкурентов в категории subject_id.

        Фильтры:
          - Исключает target_sku.
          - Рейтинг >= min_rating.
          - Сортировка: max feedbacks (DESC).
        """
        url = WB_SEARCH_URL.format(subject_id=subject_id)
        
        logger.info(
            "▶ fetch_competitors subjectId=%d top=%d minRating=%.1f",
            subject_id, top_n, min_rating,
        )

        competitors = []

        if subject_id > 0:
            data = await _get_with_retry(self._session, url, label="search-category")
            if data:
                raw_products = data.get("data", {}).get("products", [])
                candidates = []
                for p in raw_products:
                    # Фильтрация рекламы (log объект или рекламные флаги)
                    if "log" in p:
                        continue
                    flags = p.get("flags", {})
                    if flags.get("isPromo") or flags.get("isAd"):
                        continue

                    f = _extract_search_product(p)
                    if f["sku"] == target_sku:
                        continue
                    if f["rating"] < min_rating:
                        continue
                    candidates.append(f)

                candidates.sort(key=lambda x: x["feedbacks"], reverse=True)

                import string
                # Извлекаем первое существительное (упрощенно - первое слово)
                first_word = ""
                words = target_name.split()
                if words:
                    first_word = words[0].strip(string.punctuation).lower()

                semantic_matches = []
                other_matches = []

                for c in candidates:
                    if first_word and first_word in c["name"].lower():
                        semantic_matches.append(c)
                    else:
                        other_matches.append(c)

                # Добиваем семантические совпадения остальными популярными товарами категории
                final_candidates = (semantic_matches + other_matches)[:top_n]

                for c in final_candidates:
                    competitors.append(
                        CompetitorInfo(
                            sku=c["sku"],         name=c["name"],
                            brand=c["brand"],     price=c["price"],
                            rating=c["rating"],   feedbacks=c["feedbacks"],
                            url=WB_PRODUCT_URL.format(sku=c["sku"]),
                            subject_id=c["subject_id"],
                        )
                    )

        if not competitors:
            logger.warning(
                "Could not fetch competitors via Search API (probably 429 block). "
                "Falling back to DuckDuckGo search scraping."
            )
            # Фолбек через DuckDuckGo для обхода WAF и получения РЕАЛЬНЫХ конкурентов
            import urllib.parse
            import re
            
            first_word = ""
            words = target_name.split()
            import string
            if words:
                first_word = words[0].strip(string.punctuation).lower()
                
            ddg_query = f"site:wildberries.ru/catalog/ {first_word}"
            ddg_url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(ddg_query)
            
            ddg_data = await _get_with_retry(self._session, ddg_url, label="ddg-search")
            if ddg_data is None:
                # Если html.duckduckgo.com вернул текст (а _get_with_retry пытается парсить JSON и возвращает None),
                # нам нужно сделать отдельный сырой запрос:
                pass

            # Делаем сырой запрос, так как DDG отдает HTML, а _get_with_retry ждет JSON
            try:
                resp = await self._session.get(ddg_url, headers=_HEADERS, impersonate=_IMPERSONATE)
                if resp.status_code == 200:
                    html = resp.text
                    skus = list(set(re.findall(r"wildberries\.ru/catalog/(\d+)/detail", html)))
                    logger.info("DDG found SKUs: %s", skus)
                    
                    for c_sku_str in skus:
                        if len(competitors) >= top_n:
                            break
                        c_sku = int(c_sku_str)
                        if c_sku == target_sku:
                            continue
                            
                        # Получаем данные конкурента из Basket CDN
                        for basket_url in _get_basket_card_urls(c_sku):
                            c_data = await _get_with_retry(self._session, basket_url, label="basket-comp")
                            if c_data and c_data.get("imt_name"):
                                parsed = _parse_basket_card(c_sku, c_data)
                                
                                # Дополнительная проверка на совпадение слова, чтобы исключить мусор
                                if first_word and first_word not in parsed.name.lower():
                                    continue
                                
                                competitors.append(
                                    CompetitorInfo(
                                        sku=parsed.sku,
                                        name=parsed.name,
                                        brand=parsed.brand,
                                        price=parsed.price,
                                        rating=parsed.rating,
                                        feedbacks=parsed.feedbacks,
                                        url=WB_PRODUCT_URL.format(sku=parsed.sku),
                                        subject_id=parsed.subject_id,
                                    )
                                )
                                break
            except Exception as e:
                logger.error("DDG fallback failed: %s", e)

        logger.info(
            "Found %d competitors for target %d", len(competitors), target_sku
        )
        return competitors

    # ── Полный анализ ─────────────────────────────────────────────────────────

    async def analyze(self, sku: int) -> WBAnalysisData:
        """
        Полный сбор данных: карточка товара + топ конкурентов.

        Запросы последовательны (нужны данные первого для второго).
        """
        target      = await self.fetch_product(sku)
        competitors = await self.fetch_competitors(
            subject_id=target.subject_id,
            target_sku=target.sku,
            target_name=target.name,
        )
        return WBAnalysisData(target=target, competitors=competitors)
