"""The research package must not import an order client."""

import subprocess
import sys
import types
from pathlib import Path

import pytest

from pmarb.safety import assert_no_order_client


def test_core_imports_do_not_load_order_modules() -> None:
    code = """
import sys
sys.path.insert(0, sys.argv[1])
before = set(sys.modules)
import pmarb
import pmarb.calculator
import pmarb.cli
import pmarb.app.queries
import pmarb.app.decisions
import streamlit
pmarb.safety.assert_no_order_client()
loaded = [name for name in sys.modules if name not in before]
bad = [
    name
    for name in loaded
    if name != "streamlit.commands.execution_control"
    and ("orders" in name.lower() or "execution" in name.lower())
]
print("\\n".join(bad))
raise SystemExit(1 if bad else 0)
"""
    completed = subprocess.run(
        [sys.executable, "-c", code, str(Path(__file__).resolve().parents[1] / "src")],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stdout.strip() == ""


@pytest.mark.parametrize("name", ["pmarb.orders", "pmarb.execution", "broker.orders"])
def test_guard_rejects_an_injected_order_module(name: str) -> None:
    sys.modules[name] = types.ModuleType(name)
    try:
        with pytest.raises(RuntimeError, match="order submission"):
            assert_no_order_client()
    finally:
        del sys.modules[name]
