import os
import requests
import urllib.parse
from dataclasses import dataclass

@dataclass
class Product:
    sku: int
    name: str
    brand: str
    price: float
    rating: float
    feedbacks: int
    url: str
    demand_score: float = 0.0

class WBClient:
    def __init__(self, token=None, headless=True):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "*/*",
            "Origin": "https://www.wildberries.ru",
            "Referer": "https://www.wildberries.ru/"
        })

        self.token = token
        if not self.token:
            if os.path.exists(".token"):
                with open(".token", "r") as f:
                    self.token = f.read().strip()
            else:
                from get_token import get_xwb_token
                self.token = get_xwb_token(headless=headless)

        if self.token:
            self.session.cookies.set("xwb-token", self.token, domain=".wildberries.ru")
            # WB API often requires the token in a custom header as well
            self.session.headers.update({"x-pow": self.token})

    def search_products(self, query, limit=100):
        url = f"https://search.wb.ru/exactmatch/ru/common/v18/search?appType=1&curr=rub&dest=-1257786&lang=ru&query={urllib.parse.quote(query)}&resultset=catalog&sort=popular&spp=30"

        # Test generic catalog fallback if no token or token is invalid
        r = self.session.get(url)

        if r.status_code == 429:
            print("Превышен лимит запросов WB (HTTP 429). Ждем...")
            import time
            time.sleep(3)
            r = self.session.get(url)

        if r.status_code == 498:
            print("Токен xwb-token недействителен (HTTP 498). Требуется обновить токен.")
            if os.path.exists(".token"):
                os.remove(".token")
            raise Exception("Токен истек. Перезапустите скрипт для получения нового.")

        if r.status_code != 200:
            raise Exception(f"Ошибка поиска: {r.status_code}")

        data = r.json()
        products = []
        for p in data.get("data", {}).get("products", [])[:limit]:
            # Extract price (convert from kopecks to rubles)
            price = 0
            sizes = p.get("sizes", [])
            if sizes:
                price = sizes[0].get("price", {}).get("product", 0) / 100
                if price == 0:
                    price = sizes[0].get("price", {}).get("basic", 0) / 100
            if price == 0:
                price = p.get("salePriceU", 0) / 100

            products.append(Product(
                sku=p.get("id"),
                name=p.get("name", ""),
                brand=p.get("brand", ""),
                price=price,
                rating=p.get("reviewRating", 0) or p.get("rating", 0),
                feedbacks=p.get("feedbacks", 0),
                url=f"https://www.wildberries.ru/catalog/{p.get('id')}/detail.aspx"
            ))
        return products

    def find_competitors(self, article, top_n=5):
        # 1. Сначала найдем сам товар по артикулу, чтобы узнать его название
        target_results = self.search_products(str(article), limit=1)
        if not target_results:
            raise Exception(f"Товар с артикулом {article} не найден.")

        target = target_results[0]

        # 2. Формируем поисковый запрос (первые 3 слова названия)
        words = target.name.split()
        query = " ".join(words[:3]) if len(words) >= 3 else target.name

        # 3. Ищем 100 конкурентов
        all_results = self.search_products(query, limit=100)

        # 4. Фильтруем (кроме самого товара, и в диапазоне цен ±30%)
        competitors = []
        min_price = target.price * 0.7
        max_price = target.price * 1.3

        for p in all_results:
            if p.sku != target.sku and min_price <= p.price <= max_price:
                # 5. Считаем demand_score
                reviews_score = min(p.feedbacks / 1000, 1.0) * 100 * 0.4
                rating_score = (p.rating / 5.0) * 100 * 0.3
                sales_score = min((p.feedbacks * 12) / 10000, 1.0) * 100 * 0.3
                p.demand_score = reviews_score + rating_score + sales_score
                competitors.append(p)

        # 6. Сортируем по demand_score и берем топ-N
        competitors.sort(key=lambda x: x.demand_score, reverse=True)
        return target, competitors[:top_n]

    def get_rank(self, article, query):
        results = self.search_products(query, limit=500)
        for i, p in enumerate(results, 1):
            if p.sku == article:
                return i
        return -1
