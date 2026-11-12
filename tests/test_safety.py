"""The research package must not import an order client."""

import subprocess
import sys
import types

import pytest

from pmarb.safety import assert_no_order_client


def test_core_imports_do_not_load_order_modules() -> None:
    code = """
import sys
before = set(sys.modules)
import pmarb
import pmarb.calculator
pmarb.safety.assert_no_order_client()
loaded = [name for name in sys.modules if name not in before]
bad = [
    name
    for name in loaded
    if "orders" in name.lower() or "execution" in name.lower()
]
print("\\n".join(bad))
raise SystemExit(1 if bad else 0)
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stdout.strip() == ""


def test_guard_rejects_an_injected_order_module() -> None:
    sys.modules["pmarb.orders"] = types.ModuleType("pmarb.orders")
    try:
        with pytest.raises(RuntimeError, match="order submission"):
            assert_no_order_client()
    finally:
        del sys.modules["pmarb.orders"]
