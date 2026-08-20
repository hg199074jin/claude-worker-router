"""Live acceptance test: a ``25%`` discount on ``200`` must yield ``150``.

This test is committed with the buggy baseline ``discount.py`` so it fails
until the worker swaps the addition for a subtraction. The executor runs it
via ``uv run --python 3.12 python -m unittest -v`` after the worker edits.
"""

from __future__ import annotations

import unittest

from discount import compute_price


class DiscountTests(unittest.TestCase):
    def test_twenty_five_percent_discount_on_two_hundred(self):
        self.assertEqual(compute_price(200.0, 25.0), 150.0)


if __name__ == "__main__":
    unittest.main()