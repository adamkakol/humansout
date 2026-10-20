import unittest
from duration import parse_duration


class PublicTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(parse_duration("2h"), 7200)
        self.assertEqual(parse_duration("45s"), 45)
        self.assertEqual(parse_duration("1h5m10s"), 3910)
        self.assertEqual(parse_duration("90m"), 5400)

    def test_invalid(self):
        for bad in ("", "abc", "5x"):
            with self.assertRaises(ValueError):
                parse_duration(bad)
