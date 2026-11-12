# Collector deployment

Use a machine you control for long recordings. The collector is an observer; review its recorded data before drawing conclusions.

## Prerequisites and install

Install Python 3.12 or newer and Git. In a checkout with the project dependencies installed, create the virtual environment and install the package:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

Choose an absolute data directory outside the checkout and export it before setup so schema initialization and doctor use the same location:

```sh
export DATA_DIR=/absolute/path/to/data
export PMARB_DATA_DIR="$DATA_DIR"
```

Prepare the private settings file before installation. If needed, copy the example and lock down its directory and file, then edit it locally to set `PMARB_DATA_DIR` and configure the key IDs and private-key paths you use:

```sh
mkdir -p "$HOME/.config/pmarb"
chmod 700 "$HOME/.config/pmarb"
if [ ! -e "$HOME/.config/pmarb/.env" ]; then cp .env.example "$HOME/.config/pmarb/.env"; fi
chmod 600 "$HOME/.config/pmarb/.env"
```

Put key files outside the repository and restrict them to the owner (`chmod 600`). If a venue is not configured yet, clear its example private-key path. Keep credentials out of this repository, command history, and support messages.

The installer creates the settings file if it is missing, then runs `pmarb doctor`; that check requires an initialized database. Initialize the schema in the selected data directory first:

```sh
mkdir -p "$DATA_DIR/logs"
DATA_DIR="$DATA_DIR" .venv/bin/python -c 'import os; from pathlib import Path; from pmarb.storage.db import open_writer; open_writer(Path(os.environ["DATA_DIR"]) / "pmarb.sqlite").close()'
```

From the repository root, install the service with:

```sh
sh deploy/install.sh --repo "$PWD" --data-dir "$DATA_DIR"
```

Use `--dry-run` to preview installer actions. `--uninstall` removes the service definition and leaves the data directory and settings file intact. The installer keeps an existing settings file and does not overwrite it. Keep comments on separate lines in this file; systemd does not treat inline assignment comments as comments. Its doctor check verifies configuration, key files, data directory, disk, database schema, log directory, and settings permissions.

## Start, stop, and check status

The installer adds a user service and does not start it unless `--start` was requested. On macOS, load or unload the installed LaunchAgent with `launchctl`; on Linux, use the systemd user service:

```sh
# macOS (bootstrap after installation without --start; bootout to stop)
launchctl bootstrap gui/$(id -u) "$HOME/Library/LaunchAgents/com.pmarb.collector.plist"
launchctl print gui/$(id -u)/com.pmarb.collector
launchctl bootout gui/$(id -u) "$HOME/Library/LaunchAgents/com.pmarb.collector.plist"

# Linux
systemctl --user start pmarb-collector.service
systemctl --user status pmarb-collector.service
systemctl --user stop pmarb-collector.service
```

For Linux service operation after logout or reboot, enable user lingering according to the host's policy with `loginctl enable-linger`.

The database and raw recordings live under the configured data directory. Rotating application logs are in its `logs/` subdirectory (`pmarb.log` plus up to five 10 MiB backups). When `PMARB_LOG_DIR` is set, application logs go to these bounded files rather than service stdout/stderr. On Linux, service-manager messages remain available through `journalctl --user -u pmarb-collector.service`.

Run `PMARB_DATA_DIR="$DATA_DIR" .venv/bin/pmarb doctor` for a readiness check. To inspect live recorder heartbeats and errors, launch the local review app with `PMARB_DATA_DIR="$DATA_DIR" .venv/bin/pmarb review` and select **Health**. Keep the review app bound to the local machine; do not expose it to the public internet.

## Keep the machine awake and upgrade

On macOS, run `caffeinate -s` in a terminal while recording, or review `pmset` power settings for your machine. On Linux, configure the host's power policy so it does not suspend during a recording. Confirm the machine has stable network access and enough free disk space.

To upgrade, stop the service, pull the desired revision in the checkout, update the editable installation if dependencies changed, then start it again:

```sh
git pull
.venv/bin/python -m pip install -e .
```

Use the platform's stop and start commands above around the upgrade. Check `pmarb doctor`, service status, and the Health page after restart.

To move recordings between machines, stop the collector and copy the data directory directly over SSH with `rsync`, for example `rsync -a -- "$DATA_DIR/" '<ssh-destination>:<absolute-data-dir>/'`. Replace the placeholders locally with your SSH destination and absolute path. Never place recordings in the repository or commit them. Update the destination's local data-directory setting before starting its service.
