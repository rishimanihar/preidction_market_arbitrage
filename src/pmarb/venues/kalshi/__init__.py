"""Kalshi venue parsing and REST adapter."""

from pmarb.venues.kalshi.parse import is_eligible, parse_book, parse_market
from pmarb.venues.kalshi.rest import KalshiAdapter

__all__ = ["KalshiAdapter", "is_eligible", "parse_book", "parse_market"]
