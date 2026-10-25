"""Run the ingest loop as a macOS background service (a per-user LaunchAgent).

  python -m scripts.service install     write ~/Library/LaunchAgents/<label>.plist and start it
  python -m scripts.service uninstall   stop it and remove the plist
  python -m scripts.service status      is it running? last log lines
  python -m scripts.service logs        follow the log (Ctrl-C to stop)

The agent starts at login and restarts the loop if it exits. It runs under
`caffeinate -s`, which keeps the Mac awake only while it's on AC power; on battery the
Mac sleeps normally and the loop pauses until it wakes. Logs go to data/logs/ingest.log
(rotated at 10 MB, 5 files). Only one process may write the DuckDB file, so stop any
loop running in a terminal before installing.
"""
from __future__ import annotations

import os
import plistlib
import subprocess
import sys
import time
from pathlib import Path

from common.settings import ROOT

LABEL = "com.nfl-lines.ingest"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
LOG = ROOT / "data" / "logs" / "ingest.log"
CRASH_LOG = ROOT / "data" / "logs" / "ingest.stderr.log"


def plist() -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": ["/usr/bin/caffeinate", "-s", str(ROOT / ".venv" / "bin" / "python"),
                             "-W", "ignore::FutureWarning", "-m", "pipelines.ingest_odds", "--loop"],
        "WorkingDirectory": str(ROOT),
        "EnvironmentVariables": {"LOG_FILE": str(LOG), "PYTHONUNBUFFERED": "1",
                                 "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"},
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 60,          # wait at least a minute between restarts
        "ProcessType": "Background",
        "StandardOutPath": str(CRASH_LOG),
        "StandardErrorPath": str(CRASH_LOG),
    }


def _launchctl(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, check=check)


def _domain() -> str:
    return f"gui/{os.getuid()}"


def running() -> str | None:
    r = _launchctl("print", f"{_domain()}/{LABEL}")
    if r.returncode != 0:
        return None
    for line in r.stdout.splitlines():
        if line.strip().startswith("pid ="):
            return line.split("=", 1)[1].strip()
    return "loaded, not running"


def install() -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    if running():
        _launchctl("bootout", f"{_domain()}/{LABEL}")
        time.sleep(1)
    with PLIST.open("wb") as f:
        plistlib.dump(plist(), f)
    r = _launchctl("bootstrap", _domain(), str(PLIST))
    if r.returncode != 0:
        sys.exit(f"launchctl bootstrap failed: {r.stderr.strip()}")
    time.sleep(2)
    print(f"installed {PLIST}\nstatus: pid {running()}\nlog: {LOG}")


def uninstall() -> None:
    if running():
        _launchctl("bootout", f"{_domain()}/{LABEL}")
    PLIST.unlink(missing_ok=True)
    print("service stopped and removed")


def status() -> None:
    print(f"{LABEL}: {'pid ' + running() if running() else 'not installed / not running'}")
    for path in (LOG, CRASH_LOG):
        if path.exists():
            lines = path.read_text(errors="replace").splitlines()[-8:]
            print(f"--- {path.name} (last {len(lines)} lines)")
            print("\n".join(lines))


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "install":
        install()
    elif cmd == "uninstall":
        uninstall()
    elif cmd == "logs":
        os.execvp("tail", ["tail", "-n", "40", "-F", str(LOG)])
    else:
        status()


if __name__ == "__main__":
    main()
