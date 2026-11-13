# Run the pmarb collector on Windows

**This runbook is written for an AI coding assistant** working on a teammate's Windows PC. Follow it top to bottom. Every step gives:

- **Run:** the PowerShell command;
- **Expect:** what success looks like;
- **If it fails:** what to do.

If a step fails and "If it fails" does not fix it, **stop and ask the human**. Read the [Rules for the AI assistant](#rules-for-the-ai-assistant) at the end before you start.

The collector (`pmarb scan`) is a **read-only observer**. It discovers sports markets on Kalshi and Polymarket US, polls their public order books, computes cross-venue arbitrage, and records everything on this PC. **It places no orders and holds no trading credentials.**

> **Data is PRIVATE.** Everything the collector records is private: the database, raw logs, reports, and screenshots of the review app. Venue terms restrict sharing it.
> - Never commit it to git.
> - Never upload or sync it (for example, keep it out of OneDrive).
> - Never paste values from it into an issue, a PR, a commit message, or any chat other than this session with the human.
>
> You may show outputs to the human on this PC.

> **No API keys are needed for M1.** Everything in this runbook uses public endpoints. In M2 the human will create read-only keys. They go in files under `%USERPROFILE%\.config\pmarb\` with restricted ACLs (see [API keys (M2 only)](#api-keys-m2-only)). **Never paste a key, a key id, or the settings file into an AI chat**, including this one.

## Conventions

- Use **Windows PowerShell 5.1** ("Windows PowerShell" in the Start menu) or PowerShell 7, as the **normal (non-administrator) user** who will keep the collector running. Do not use an elevated window unless a step says so.
- Run every command from the checkout folder, `$repo`, which step 4 sets. If you open a new window, run `Set-Location $repo` again; if `$repo` is empty there, set it again as in step 4.
- `pmarb` means `.\.venv\Scripts\pmarb.exe`. Activating the virtual environment is never needed.
- Times in the database are UTC.

## Where things live

| What | Where |
|---|---|
| Checkout | `$repo`, for example `%USERPROFILE%\src\pmarb`. Keep the path short and out of OneDrive. |
| Virtual environment | `$repo\.venv` |
| Settings file | `%USERPROFILE%\.config\pmarb\.env`, readable only by this user |
| Data directory (private, gitignored) | `$repo\data` by default |
| Database | `$repo\data\pmarb.sqlite` (with `-wal` and `-shm` files next to it) |
| Raw responses | `$repo\data\raw\<venue>\<date>\<hour>.jsonl.gz` |
| Application log (rotating, 10 MiB x 6) | `$repo\data\logs\pmarb.log` |
| Wrapper log, crash output | `$repo\data\logs\collector.wrapper.log`, `collector.stderr.log`, `collector.stdout.log` |
| Scheduled task | Task Scheduler Library > `pmarb-collector` |

---

## 1. Allow scripts for this PowerShell process only

**Run:**
```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned -Force
Get-ExecutionPolicy -Scope Process
```
**Expect:** `RemoteSigned`. This lasts only until the window closes; nothing machine-wide changes.

**If it fails:** if the message says the setting is "overridden by a policy defined at a more specific scope", an organization policy controls scripts. Stop and ask the human. Do not change the `CurrentUser` or `LocalMachine` scopes.

## 2. Check Git

**Run:**
```powershell
git --version
```
**Expect:** `git version 2.<something>`.

**If it fails** (`git` is not recognized): install it, close this window, open a new one, and repeat step 1.
```powershell
winget install -e --id Git.Git
```
If `winget` is missing, ask the human to install Git from https://git-scm.com/download/win.

## 3. Check Python 3.12

**Run:**
```powershell
py -3.12 --version
```
**Expect:** `Python 3.12.<n>`. The project needs 3.12 and is tested on 3.12.

**If it fails:**
- `py` is not recognized, or it reports that no 3.12 runtime is installed: install Python 3.12, close this window, open a new one, and repeat step 1.
  ```powershell
  winget install -e --id Python.Python.3.12
  ```
  Without `winget`, ask the human to run the 3.12 installer from https://www.python.org/downloads/windows/ with **"py launcher"** ticked.
- Do not use the `python` command from the Microsoft Store alias. Always use `py -3.12`.

## 4. Clone the repository and check out the branch

Pick a short folder that **is not synced by OneDrive**. `%USERPROFILE%\src` is a good default. `Documents` and `Desktop` are often redirected to OneDrive.

**Run:**
```powershell
New-Item -ItemType Directory -Force -Path "$env:USERPROFILE\src" | Out-Null
Set-Location "$env:USERPROFILE\src"
git clone https://github.com/rishimanihar/preidction_market_arbitrage.git pmarb
Set-Location pmarb
git checkout m1/windows
$repo = (Get-Location).Path
git status
$repo -like '*OneDrive*'
```
Use `m1/windows` until that pull request is merged; after the merge, use `main`. If the human named a different branch, use theirs.

**Expect:**
- `git status` says `On branch m1/windows` and `nothing to commit, working tree clean`.
- The last line is `False`, meaning the checkout is not in OneDrive.

**If it fails:**
- The folder already exists: reuse it with `Set-Location "$env:USERPROFILE\src\pmarb"`, then `git fetch`, `git checkout m1/windows`, and `git status`. If `git status` shows local changes, stop and ask the human. Never discard their changes.
- The last line is `True`: pick another folder outside OneDrive and repeat.
- "Filename too long": run `git config --global core.longpaths true`, then use a shorter folder.
- Network or proxy errors: ask the human.

## 5. Dry-run the installer

**Run:**
```powershell
.\deploy\windows\install.ps1 -DryRun
```
**Expect:**
- Header lines `Task: pmarb-collector`, `Repo: ...`, `Data: ...\data`, `Settings: ...\.config\pmarb\.env`.
- Then `OK Python 3.12.<n>`, followed by `DRY-RUN would ...` lines.

Nothing is changed.

**If it fails:**
- "running scripts is disabled": repeat step 1 in this window.
- "py -3.12 did not find Python 3.12": go back to step 3.
- "git was not found": go back to step 2.

## 6. Install

**Run** (it takes a few minutes; pip downloads the dependencies):
```powershell
.\deploy\windows\install.ps1
```
The installer:

1. creates `.venv` with Python 3.12;
2. runs `pip install -e .[dev]`;
3. creates `%USERPROFILE%\.config\pmarb\`;
4. creates `.env` from `.env.example` **only if `.env` does not exist**. A new file gets `PMARB_DATA_DIR` set and the example key paths cleared;
5. restricts `.env` to this user with `icacls`;
6. creates `data\` and `data\logs\` and initializes the database;
7. runs `pmarb doctor`;
8. registers the Task Scheduler task `pmarb-collector`, without starting it.

The task starts the collector at logon, restarts it one minute after a crash, and has no time limit.

**Expect:**
- A `pmarb doctor` block ending in `OK config, data dir, disk, keys, database schema, Streamlit, time zones, logs, and env file`.
- Then `OK installed.` and `Not started.`.

**If it fails:**
- "pmarb doctor reported FAIL lines": fix them with the table in step 8, then run the installer again. It is safe to rerun.
- "Task pmarb-collector is running": stop it with step 13 first.
- `pip` network errors: run the installer again. If they persist, ask the human about a proxy.
- `Register-ScheduledTask` says "Access is denied": an organization policy blocks user tasks. Ask the human. The collector can still run by hand in a window that stays open: `.\.venv\Scripts\pmarb.exe scan`. Stop it with Ctrl+C or `.\.venv\Scripts\pmarb.exe stop --wait 90` from another window.
- A warning that `.env` sets a different `PMARB_DATA_DIR`: ask the human which data folder is right. Then make the `PMARB_DATA_DIR=` line in `.env` match the `-DataDir` you install with.

## 7. Fee settings: leave the defaults unless the human knows the values

The collector needs two **account-specific** fee parameters. **Do not guess them.** Leave them unset unless the human tells you the exact values from their venue accounts.

| Setting (in `.env`) | Allowed values | Unset (default) means |
|---|---|---|
| `PMARB_KALSHI_MEMBER_PRECISION` | `0.0001` (direct member) or `0.01` | the conservative stand-in `0.01` |
| `PMARB_POLYUS_THETA` | `0.095`, `0.085`, `0.075`, `0.0695` | the conservative stand-in `0.095` |
| `PMARB_FEE_SCENARIO` | `conservative` (default) or `none` | `conservative` |

**Why the defaults are safe.** With `PMARB_FEE_SCENARIO=conservative` and the account values unset, every fee is priced with the most expensive published choice:

- Kalshi's coarser `0.01` rounding grid;
- Polymarket US's highest tier, Θ `0.095`.

These are upper bounds. An edge that survives them survives the real fees. The results are tagged `scenario:` and always stay *indicative*, never *qualified*. Setting `PMARB_FEE_SCENARIO=none` turns this off, so every assessment becomes `fee_unknown` and no episodes open. Do not use `none` for a soak.

**Run** (shows only these non-secret lines, never the whole file):
```powershell
Select-String -Path "$env:USERPROFILE\.config\pmarb\.env" -Pattern '^PMARB_(DATA_DIR|FEE_SCENARIO|KALSHI_MEMBER_PRECISION|POLYUS_THETA)='
```
**Expect:** `PMARB_DATA_DIR=<your data folder>`, and usually no fee lines, which means the defaults apply.

**If the human gives you values:** ask the human to add them with `notepad "$env:USERPROFILE\.config\pmarb\.env"`, one per line and with no inline comments, for example `PMARB_POLYUS_THETA=0.075`. Then repeat step 8. Never print or paste the whole settings file.

## 8. `pmarb doctor` must be all green

**Run:**
```powershell
.\.venv\Scripts\pmarb.exe doctor
$LASTEXITCODE
```
**Expect:**
- The last line before the exit code is `OK config, data dir, disk, keys, database schema, Streamlit, time zones, logs, and env file`.
- `$LASTEXITCODE` is `0`.
- These lines are informational and fine:
  - `INFO clock source ... (w32tm)`
  - `WARN clock offset unknown`
  - `INFO disk headroom ...`
  - `WARN clock synchronization unknown; w32tm /query /status failed: ... Access is denied`

Any `FAIL` line makes the exit code `1`.

**If it fails:**

| Output starts with | Fix |
|---|---|
| `FAIL time zone data missing` | `.\.venv\Scripts\python.exe -m pip install -e .`, which installs the pinned `tzdata`. |
| `FAIL env file is readable by ...` | Restrict the file. Run `$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value`, then `icacls "$env:USERPROFILE\.config\pmarb\.env" /inheritance:r /grant:r "*${sid}:(F)"`. |
| `FAIL env file must live under ...` | The settings file must be `%USERPROFILE%\.config\pmarb\.env`. Clear any `PMARB_ENV_FILE` variable (`Remove-Item Env:PMARB_ENV_FILE`) or move the file there. |
| `FAIL env file ACL could not be verified` | Run `icacls "$env:USERPROFILE\.config\pmarb\.env"` and show the human the output. |
| `WARN env file ACL NOT verified: icacls was not found` | Check with `Get-Command icacls`; it should be `C:\Windows\System32\icacls.exe`. Ask the human. |
| `Kalshi key file check failed` or `Polymarket US key file check failed` | M1 needs no keys. Ask the human to empty that line in `.env`, for example `KALSHI_PRIVATE_KEY_PATH=`. |
| `FAIL database not found` or `database schema ...` | Run `.\deploy\windows\install.ps1` again; it initializes the database. |
| `WARN logs directory not found` | Expected before the first collector run; it creates the directory. A logs directory that is not writable is still a `FAIL`. |
| `FAIL data dir is not gitignored` or `data dir inside repository must be under ...` | Use the default data folder (`$repo\data`) or a folder outside the checkout. |
| `FAIL disk headroom below 2 GiB` | Ask the human to free disk space. A soak writes several GB per day. |
| `WARN system clock is not synchronized` | Ask the human to open Settings > Time & language > Date & time, turn on "Set time automatically", and click "Sync now". |

## 9. Keep the PC awake (the human decides)

A sleeping PC records nothing. The collector notices the gap (a clock jump) and closes open episodes, but the soak loses that time. **The installer never changes power settings, and you must not either without the human's approval.**

**Run** (read-only check):
```powershell
powercfg /query SCHEME_CURRENT SUB_SLEEP STANDBYIDLE
```
**Expect:** the "Current AC Power Setting Index" is the sleep timeout in seconds while plugged in. `0x00000000` means never.

**If the human approves**, these commands disable sleep and hibernate on AC power. They may need an elevated PowerShell.
```powershell
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
```
The display may still turn off. Also tell the human:

- Windows Update can restart the PC. Set "active hours" in Settings > Windows Update > Advanced options.
- After a restart, the task starts only when the human logs on, one minute after logon. That is the trade-off of running as the logged-on user with no stored password.

## 10. Start the collector

**Run:**
```powershell
Start-ScheduledTask -TaskName pmarb-collector
Start-Sleep -Seconds 15
Get-ScheduledTask -TaskName pmarb-collector | Select-Object TaskName, State
Get-Content "$repo\data\logs\collector.wrapper.log" -Tail 3
```
**Expect:** `State` is `Running`, and the wrapper log's last line ends with `starting pmarb scan`.

**If it fails** (`State` is `Ready`, or the wrapper log shows `exited with code` and a non-zero number):
```powershell
Get-Content "$repo\data\logs\collector.stderr.log" -Tail 40
Get-ScheduledTaskInfo -TaskName pmarb-collector | Select-Object LastRunTime, LastTaskResult
```
Show the human the error. After a crash the wrapper restarts the collector every 60 seconds. Do not edit code to work around an error.

## 11. Verify after 5 minutes

**Run:**
```powershell
Start-Sleep -Seconds 300
Get-ScheduledTask -TaskName pmarb-collector | Select-Object State
Select-String -Path "$repo\data\logs\collector.wrapper.log" -Pattern 'exited with code' | Select-Object -Last 3
```
**Expect:** `State` is `Running`, and no new `exited with code` lines since step 10.

Then check the database with Python (Windows has no `sqlite3` command). The query is read-only and prints only counts and timestamps, never market values:
```powershell
@'
from contextlib import closing
from pmarb.config import load_settings
from pmarb.storage.db import open_reader

db = load_settings().data_dir / "pmarb.sqlite"
recent = "strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-10 minutes')"
with closing(open_reader(db)) as conn:
    print("database", db)
    for table in ("markets", "groups", "assessments", "episodes", "collector_events"):
        print(table, conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    print("last_event_utc", conn.execute("SELECT MAX(utc) FROM collector_events").fetchone()[0])
    print("now_utc", conn.execute("SELECT strftime('%Y-%m-%dT%H:%M:%SZ', 'now')").fetchone()[0])
    query = f"SELECT kind, COUNT(*) FROM collector_events WHERE utc >= {recent} GROUP BY kind"
    for kind, count in conn.execute(query + " ORDER BY kind"):
        print("last_10_min", kind, count)
'@ | .\.venv\Scripts\python.exe -
```
**Expect:**
- `markets` and `groups` are greater than 0 after the first discovery pass.
- `last_event_utc` is within about a minute of `now_utc`.
- `last_10_min health` is roughly 30 or more (one heartbeat every 10 seconds).
- `last_10_min timing` is greater than 0.
- `assessments` and `episodes` may be 0 when no games are close to starting. That is normal.

**If it fails:**

| Symptom | Meaning and next step |
|---|---|
| `last_10_min adapter_error` is in the dozens | Network trouble or rate limiting. Show the human `Get-Content "$repo\data\logs\pmarb.log" -Tail 50`. Read that file with `-Tail`, never `-Wait`, and do not keep it open in an editor: an open handle blocks log rotation on Windows. |
| `last_10_min recording_backpressure` appears repeatedly | The disk cannot keep up. Show the human the disk free space and these counts. |
| `last_event_utc` is old | The collector is not writing. Go back to step 10's "If it fails". |

Then produce the M1 report for the last 10 minutes and show it to the human. Do not paste it anywhere else.
```powershell
$since = (Get-Date).ToUniversalTime().AddMinutes(-10).ToString('yyyy-MM-ddTHH:mm:ssZ')
.\.venv\Scripts\pmarb.exe report --since $since --out -
```
**Expect:** a Markdown report. In its `## Coverage` table, `events` is greater than 0, and `http_429_count` and `error_count` are small. Without `--out -` the report is saved under `data\reports\`, and the command prints that path.

## 12. Open the review app (local only)

The command keeps running. Run it in a **separate** PowerShell window (`Set-Location $repo` first), or ask the human to.

**Run:**
```powershell
$env:STREAMLIT_SERVER_HEADLESS = 'true'
$env:STREAMLIT_BROWSER_GATHER_USAGE_STATS = 'false'
.\.venv\Scripts\pmarb.exe review
```
**Expect:** Streamlit prints `URL: http://127.0.0.1:8501`. Open that address in a browser on this PC, then select **Health** for heartbeats and errors. Stop the app with Ctrl+C. The collector keeps running.

**If it fails:**
- Port 8501 is busy: run `.\.venv\Scripts\pmarb.exe review --port 8502`.
- A Windows Firewall prompt appears for `python.exe`: choose **Cancel**. The app listens on `127.0.0.1` only and needs no inbound access.
- Never bind it to another address. Never forward the port. Never expose it to the network.

## 13. Stop the collector cleanly

**Run:**
```powershell
.\deploy\windows\stop.ps1
$LASTEXITCODE
```
The script runs `pmarb stop --wait 90`. The collector notices the request within a second. It then:

1. closes open episodes with `collector_shutdown`;
2. drains the raw log and the database;
3. exits.

**Expect:**
- `OK stop requested: ...\data\collector.stop`
- `OK collector stopped: writes drained and open episodes closed`
- `OK collector stopped cleanly; task state: Ready`
- `$LASTEXITCODE` is `0`.

The wrapper log ends with `pmarb scan exited with code 0` and `clean stop; not restarting`.

**If it fails:**
- No confirmation within 90 seconds: show the human `Get-Content "$repo\data\logs\pmarb.log" -Tail 50`, then run `stop.ps1` again.
- Only if it is stuck and the human agrees, use `Stop-ScheduledTask -TaskName pmarb-collector`. That kills the process without draining. Open episodes are then closed as `collector_restart` on the next start.

To keep it from starting at the next logon, run `Disable-ScheduledTask -TaskName pmarb-collector`. `Enable-ScheduledTask` undoes that.

## 14. Upgrade

**Run:**
```powershell
.\deploy\windows\stop.ps1
git status
git pull --ff-only
.\deploy\windows\install.ps1
Start-ScheduledTask -TaskName pmarb-collector
```
**Expect:**
- `stop.ps1` succeeds.
- `git status` is clean.
- `git pull` fast-forwards.
- The installer ends with `OK installed.`; it keeps `.env` and the data folder.
- The task is `Running` again. Then repeat step 11.

**If it fails:**
- `git status` shows local changes, or `git pull` reports conflicts or "Not possible to fast-forward": stop and ask the human. Do not stash, reset, or discard anything.
- The installer's doctor reports a FAIL: use step 8.

To remove the task (data and settings are kept):
```powershell
.\deploy\windows\install.ps1 -Uninstall
```

## API keys (M2 only)

M1 needs none. When the human sets up M2:

- The human downloads each **read-only** private key from the venue and saves it under `%USERPROFILE%\.config\pmarb\`, for example `kalshi_private_key.pem`.
- You never open, print, or copy key files, and nobody pastes keys or key ids into an AI chat.
- Restrict each key file to the current user, then check it:
  ```powershell
  $sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
  $key = "$env:USERPROFILE\.config\pmarb\kalshi_private_key.pem"
  icacls $key /inheritance:r /grant:r "*${sid}:(R)"
  icacls $key
  ```
  **Expect:** exactly one entry, for the human's account, with `(R)`.
- The human adds the key id and path lines to `.env` in Notepad. Then `pmarb doctor` must stay green.
- Doctor refuses a key file in any of these cases:
  - Everyone, Users, BUILTIN\Users or Authenticated Users can read it;
  - it is not under `%USERPROFILE%\.config\pmarb\`;
  - it is inside the repository.

## Rules for the AI assistant

> 1. **Do not change code, tests, settings defaults, or CI to make a check pass.** If something fails, show the human the exact output and stop.
> 2. **Never disable or bypass git hooks, and never use `--no-verify`.** Never commit `data\`, `.env`, key files, logs, or reports.
> 3. **Ask the human before any `git push`**, and before any commit or branch change beyond what this runbook says.
> 4. **Report the `pmarb doctor` output (step 8) and the `pmarb report` output (step 11) to the human**, in this session only.
> 5. Data is private. Never upload it, sync it, or paste values from it anywhere else.
> 6. Never print or paste the settings file or key files. Never ask the human to paste a key or key id.
> 7. Do not run `powercfg`, change the execution policy beyond `-Scope Process`, or use an administrator window without the human's approval.
> 8. Use only `stop.ps1` or `pmarb stop` to stop the collector. "End task" and `Stop-ScheduledTask` are a last resort, and only with the human's approval.
