import unittest
from intervals import merge_intervals


class PublicTests(unittest.TestCase):
    def test_examples(self):
        self.assertEqual(merge_intervals([[1, 3], [2, 6], [8, 10]]), [[1, 6], [8, 10]])
        self.assertEqual(merge_intervals([[1, 4], [4, 5]]), [[1, 5]])
