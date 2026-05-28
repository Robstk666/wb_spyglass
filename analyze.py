import argparse
import sys
import json

from wb_api import WBClient

def main():
    parser = argparse.ArgumentParser(description="WB Competitor Analyzer")
    parser.add_argument("article", type=int, help="Артикул товара")
    parser.add_argument("--token", type=str, help="Готовый xwb-token (опционально)")
    parser.add_argument("--json", action="store_true", help="Сохранить отчёт в JSON")
    parser.add_argument("--top", type=int, default=5, help="Количество конкурентов (по умолчанию 5)")
    parser.add_argument("--no-headless", action="store_true", help="Не скрывать браузер при получении токена")
    args = parser.parse_args()

    client = WBClient(token=args.token, headless=not args.no_headless)

    print(f"Анализ товара {args.article}...")
    try:
        target, competitors = client.find_competitors(args.article, top_n=args.top)
    except Exception as e:
        print(f"Ошибка: {e}")
        sys.exit(1)

    print("\n" + "="*70)
    print("🎯 ВАШ ТОВАР")
    print("="*70)
    print(f"{target.name}")
    print(f"💰 {target.price} ₽   ⭐ {target.rating}   💬 {target.feedbacks}")
    print(f"🔗 {target.url}\n")

    print("="*70)
    print(f"📋 ТОП-{len(competitors)} КОНКУРЕНТОВ (по demand_score)")
    print("="*70)

    prices = [c.price for c in competitors]
    ratings = [c.rating for c in competitors]
    scores = [c.demand_score for c in competitors]

    for i, c in enumerate(competitors, 1):
        print(f"{i}. {c.name}")
        print(f"   💰 {c.price} ₽   ⭐ {c.rating}   💬 {c.feedbacks}")
        print(f"   📊 Спрос: {c.demand_score:.1f}/100   🏪 {c.brand}")
        print(f"   🔗 {c.url}\n")

    if prices:
        print("="*70)
        print("📊 АНАЛИТИКА РЫНКА")
        print("="*70)
        print(f"Средняя цена конкурентов : {sum(prices)/len(prices):.0f} ₽")
        print(f"Диапазон цен             : {min(prices)} – {max(prices)} ₽")
        print(f"Средний рейтинг          : {sum(ratings)/len(ratings):.2f} / 5.0")
        print(f"Средний demand_score     : {sum(scores)/len(scores):.1f} / 100")

        diff = target.price - sum(prices)/len(prices)
        if diff > 0:
            print(f"Ваша цена vs рынок       : {diff:.0f} ₽ выше среднего")
        else:
            print(f"Ваша цена vs рынок       : {-diff:.0f} ₽ ниже среднего")

    if args.json:
        report = {
            "target": target.__dict__,
            "competitors": [c.__dict__ for c in competitors],
            "analytics": {
                "avg_price": sum(prices)/len(prices) if prices else 0,
                "min_price": min(prices) if prices else 0,
                "max_price": max(prices) if prices else 0,
                "avg_rating": sum(ratings)/len(ratings) if ratings else 0,
                "avg_score": sum(scores)/len(scores) if scores else 0
            }
        }
        with open("report.json", "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print("\nОтчёт сохранён в report.json")

if __name__ == "__main__":
    main()
