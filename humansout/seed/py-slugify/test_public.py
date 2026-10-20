import unittest
from slugify import slugify


class PublicTests(unittest.TestCase):
    def test_examples(self):
        self.assertEqual(slugify("Hello, World!"), "hello-world")
        self.assertEqual(slugify("  many   spaces  "), "many-spaces")
