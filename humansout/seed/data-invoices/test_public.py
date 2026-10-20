import csv
import re
import unittest


class PublicTests(unittest.TestCase):
    def test_format(self):
        with open("/work/invoices.csv", newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(list(rows[0].keys()), ["invoice_no", "date", "vendor", "total"])
        self.assertEqual(len(rows), 10)
        for row in rows:
            self.assertRegex(row["date"], r"^\d{4}-\d{2}-\d{2}$")
            self.assertRegex(row["total"], r"^\d+\.\d{2}$")
