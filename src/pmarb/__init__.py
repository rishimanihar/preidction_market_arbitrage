"""Short-term prediction-market arbitrage research toolkit."""

from importlib.metadata import version

from pmarb.safety import assert_no_order_client

__version__ = version("pmarb")

__all__ = ["__version__", "assert_no_order_client"]
