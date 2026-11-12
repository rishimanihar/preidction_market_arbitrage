"""Import guard for the research package.

Observation and calculation must not load an order-submission client.
The check is name-based: a module whose name contains ``orders`` or
``execution`` fails it. The baseline is the module table when this module
starts loading, so a test runner's own modules are not blamed on ``pmarb``.
Every ``pmarb`` module is checked even if it was already loaded. Streamlit's
known rerun/stop control module is allowed; it does not submit venue orders.
"""

import sys

_FORBIDDEN = ("orders", "execution")
_ALLOWED = frozenset({"streamlit.commands.execution_control"})
_BASELINE = set(sys.modules)


def assert_no_order_client() -> None:
    """Raise if an order-submission or execution module has been imported."""
    candidates = set(sys.modules) - _BASELINE
    candidates.update(name for name in sys.modules if name == "pmarb" or name.startswith("pmarb."))
    offenders = sorted(name for name in candidates if name and _forbidden(name))
    if offenders:
        joined = ", ".join(offenders)
        raise RuntimeError(f"order submission modules must not be imported: {joined}")


def _forbidden(name: str) -> bool:
    if name in _ALLOWED:
        return False
    return any(part in token for token in name.lower().split(".") for part in _FORBIDDEN)


assert_no_order_client()
