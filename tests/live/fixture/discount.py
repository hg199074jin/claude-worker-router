"""Discount pricing helper used by the live smoke-test fixture.

The ``compute_price`` function carries a deliberate sign bug for the live
acceptance run: it should subtract the percentage, but the baseline commits
an addition. The smoke-test driver and ``test_discount.py`` together prove
that the worker corrects the sign and ``python -m unittest -v`` passes.
"""

from __future__ import annotations


def compute_price(price: float, percent: float) -> float:
    """Apply ``percent`` as a discount and return the discounted price.

    The baseline implementation has a deliberate sign bug so the smoke
    fixture starts failing on purpose. The worker must change ``+`` to
    ``-`` for the executor's test run to return exit code ``0``.
    """
    return price + price * (percent / 100.0)


if __name__ == "__main__":
    print(compute_price(200.0, 25.0))