"""Venue taker-fee models.

``FeeQuote.amount is None`` means the fee is unknown. Unknown is not zero.
"""

from pmarb.fees.kalshi import KalshiFeeModel
from pmarb.fees.polymarket_us import PolymarketUSFeeModel
from pmarb.safety import assert_no_order_client

__all__ = ["KalshiFeeModel", "PolymarketUSFeeModel"]

assert_no_order_client()
