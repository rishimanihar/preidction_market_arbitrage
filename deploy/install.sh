#!/bin/sh
set -eu

usage() {
  cat <<'EOF'
Usage: install.sh [--repo PATH] [--data-dir PATH] [--start] [--dry-run] [--uninstall]
Defaults: --repo is the parent of deploy/, --data-dir is REPO/data.
EOF
}

SCRIPT_DIR=$(CDPATH='' cd "$(dirname "$0")" && pwd -P)
DEFAULT_REPO=$(CDPATH='' cd "$SCRIPT_DIR/.." && pwd -P)
REPO=$DEFAULT_REPO
DATA_DIR=
START=0
DRY_RUN=0
UNINSTALL=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --repo)
      [ "$#" -ge 2 ] || { usage >&2; exit 2; }
      REPO=$2
      shift 2
      ;;
    --data-dir)
      [ "$#" -ge 2 ] || { usage >&2; exit 2; }
      DATA_DIR=$2
      shift 2
      ;;
    --start) START=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'Unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
done

case "$REPO" in -*) REPO=./$REPO ;; esac
REPO=$(CDPATH='' cd "$REPO" && pwd -P)
if [ -z "$DATA_DIR" ]; then DATA_DIR=$REPO/data; fi
case "$DATA_DIR" in
  /*) ;;
  *) DATA_DIR=$PWD/$DATA_DIR ;;
esac
DATA_DIR=$(python3 -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$DATA_DIR")
LOG_DIR=$DATA_DIR/logs
PMARB_BIN=$REPO/.venv/bin/pmarb
PYTHON_BIN=$REPO/.venv/bin/python
ENV_FILE=${HOME:?HOME must be set}/.config/pmarb/.env
CONFIG_DIR=$HOME/.config/pmarb

python3 -c 'import sys; raise SystemExit(any(any(c in p for c in "\r\n\0") for p in sys.argv[1:]))' \
  "$REPO" "$DATA_DIR" "$HOME" || { printf 'Paths cannot contain line breaks or NUL bytes.\n' >&2; exit 2; }

OS=$(uname -s)
case "$OS" in
  Darwin)
    PLATFORM=macos
    SERVICE_DIR=$HOME/Library/LaunchAgents
    SERVICE_FILE=$SERVICE_DIR/com.pmarb.collector.plist
    ;;
  Linux)
    PLATFORM=linux
    SERVICE_DIR=$HOME/.config/systemd/user
    SERVICE_FILE=$SERVICE_DIR/pmarb-collector.service
    ;;
  *) printf 'Unsupported operating system: %s\n' "$OS" >&2; exit 1 ;;
esac

run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    printf 'DRY-RUN'
    for arg do printf ' %s' "$(printf '%s' "$arg" | sed "s/'/'\\\\''/g")"; done
    printf '\n'
  else
    "$@"
  fi
}

if [ "$UNINSTALL" -eq 1 ]; then
  if [ "$PLATFORM" = macos ]; then
    run launchctl bootout "gui/$(id -u)" "$SERVICE_FILE" || :
  else
    run systemctl --user disable --now pmarb-collector.service || :
  fi
  run rm -f "$SERVICE_FILE"
  if [ "$PLATFORM" = linux ]; then run systemctl --user daemon-reload; fi
  exit 0
fi

if [ "$DRY_RUN" -eq 1 ]; then
  printf 'OS: %s\nService: %s\nRepo: %s\nData: %s\nLogs: %s\nEnv file: %s\n' \
    "$PLATFORM" "$SERVICE_FILE" "$REPO" "$DATA_DIR" "$LOG_DIR" "$ENV_FILE"
  printf 'Would create mode-700 config/data/log directories and mode-600 env/service files.\n'
  printf 'Would run: PMARB_DATA_DIR=%s PMARB_ENV_FILE=%s %s doctor\n' "$DATA_DIR" "$ENV_FILE" "$PMARB_BIN"
  start_note=
  if [ "$START" -eq 1 ]; then
    start_note=' and start it'
  fi
  printf 'Would install service without starting it%s.\n' "$start_note"
  exit 0
fi

[ -x "$PMARB_BIN" ] || { printf 'Missing executable: %s\n' "$PMARB_BIN" >&2; exit 1; }
[ -x "$PYTHON_BIN" ] || { printf 'Missing executable: %s\n' "$PYTHON_BIN" >&2; exit 1; }

umask 077
mkdir -p "$CONFIG_DIR" "$DATA_DIR" "$LOG_DIR" "$SERVICE_DIR"
chmod 700 "$CONFIG_DIR" "$DATA_DIR" "$LOG_DIR"
if [ ! -e "$ENV_FILE" ]; then
  cp "$REPO/.env.example" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
fi

if ! PMARB_DATA_DIR=$DATA_DIR PMARB_LOG_DIR=$LOG_DIR PMARB_ENV_FILE=$ENV_FILE \
  "$PMARB_BIN" doctor; then
  printf 'pmarb doctor failed; service was not installed.\n' >&2
  exit 1
fi

if [ "$PLATFORM" = macos ]; then
  TEMPLATE=$SCRIPT_DIR/macos/com.pmarb.collector.plist.template
else
  TEMPLATE=$SCRIPT_DIR/linux/pmarb-collector.service.template
fi
python3 "$SCRIPT_DIR/render_template.py" "$PLATFORM" "$TEMPLATE" "$SERVICE_FILE" \
  "REPO_DIR=$REPO" "DATA_DIR=$DATA_DIR" "LOG_DIR=$LOG_DIR" \
  "DATA_ENV=PMARB_DATA_DIR=$DATA_DIR" "LOG_ENV=PMARB_LOG_DIR=$LOG_DIR" \
  "PYTHON_BIN=$PYTHON_BIN" "ENV_FILE=$ENV_FILE"

if [ "$PLATFORM" = macos ]; then
  if [ "$START" -eq 1 ]; then
    launchctl bootout "gui/$(id -u)" "$SERVICE_FILE" >/dev/null 2>&1 || :
    launchctl bootstrap "gui/$(id -u)" "$SERVICE_FILE"
  fi
else
  systemctl --user daemon-reload
  if [ "$START" -eq 1 ]; then systemctl --user enable --now pmarb-collector.service; fi
fi
printf 'Installed %s.\n' "$SERVICE_FILE"
