import csv
import unittest


class PublicTests(unittest.TestCase):
    def test_format(self):
        with open("/work/dates.csv", newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(list(rows[0].keys()), ["id", "date"])
        self.assertEqual([r["id"] for r in rows], [str(i) for i in range(1, 13)])
        for row in rows:
            self.assertRegex(row["date"], r"^\d{4}-\d{2}-\d{2}$")
