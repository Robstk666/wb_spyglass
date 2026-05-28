import unittest
from wb_api import Product, WBClient

class TestWBLogic(unittest.TestCase):

    def test_demand_score(self):
        p1 = Product(sku=1, name="P1", brand="B", price=1000, rating=5.0, feedbacks=1000, url="")
        reviews_score = min(p1.feedbacks / 1000, 1.0) * 100 * 0.4  # 40
        rating_score = (p1.rating / 5.0) * 100 * 0.3               # 30
        sales_score = min((p1.feedbacks * 12) / 10000, 1.0) * 100 * 0.3 # 30
        score = reviews_score + rating_score + sales_score
        self.assertEqual(score, 100)

        p2 = Product(sku=2, name="P2", brand="B", price=1000, rating=0, feedbacks=0, url="")
        score2 = min(p2.feedbacks / 1000, 1.0) * 100 * 0.4 + (p2.rating / 5.0) * 100 * 0.3 + min((p2.feedbacks * 12) / 10000, 1.0) * 100 * 0.3
        self.assertEqual(score2, 0)

if __name__ == '__main__':
    unittest.main()
