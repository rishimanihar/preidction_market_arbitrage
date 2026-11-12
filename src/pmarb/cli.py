"""Command line interface. Network commands arrive in a later phase."""

import argparse

from pmarb import __version__


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="pmarb")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("scan", "review", "replay"):
        commands.add_parser(name)
    args = parser.parse_args(argv)
    if args.command in {"scan", "review", "replay"}:
        print("not implemented yet (phase 2)")
        raise SystemExit(2)
    raise SystemExit(2)


if __name__ == "__main__":
    main()
