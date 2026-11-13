"""Entry point used by unattended collector services."""

import os

from pmarb.cli import main
from pmarb.logging import configure_logging


def run() -> None:
    """Configure bounded file logging, then run the collector command."""
    configure_logging(log_dir=os.environ.get("PMARB_LOG_DIR"))
    main(["scan"])


if __name__ == "__main__":
    run()
