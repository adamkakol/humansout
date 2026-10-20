import csv
import unittest


class PublicTests(unittest.TestCase):
    def test_format(self):
        with open("/work/weights.csv", newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(list(rows[0].keys()), ["product", "weight_g"])
        self.assertEqual(len(rows), 12)
        for row in rows:
            int(row["weight_g"])
