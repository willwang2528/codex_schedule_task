#!/usr/bin/env python3
"""Install, start, inspect, or stop the macOS Automation Hub scheduler."""

from __future__ import annotations

import argparse
import os
import plistlib
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

from task_runtime import atomic_write_text


LABEL = "com.will.automation-hub.scheduler"


def launch_agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def launch_domain() -> str:
    return f"gui/{os.getuid()}"


def plist_payload(repo_root: Path) -> Dict[str, Any]:
    log_directory = repo_root / "logs" / "scheduler"
    return {
        "Label": LABEL,
        "ProgramArguments": [
            "/usr/bin/caffeinate",
            "-i",
            sys.executable,
            str(repo_root / "scripts" / "scheduler.py"),
            "--daemon",
            "--poll-seconds",
            "30",
        ],
        "WorkingDirectory": str(repo_root),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "ThrottleInterval": 10,
        "EnvironmentVariables": {
            "PATH": "/Applications/ChatGPT.app/Contents/Resources:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
        },
        "StandardOutPath": str(log_directory / "scheduler.stdout.log"),
        "StandardErrorPath": str(log_directory / "scheduler.stderr.log"),
    }


def _run_launchctl(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["launchctl", *arguments],
        capture_output=True,
        text=True,
        check=check,
    )


def install(repo_root: Path) -> None:
    target = launch_agent_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    (repo_root / "logs" / "scheduler").mkdir(parents=True, exist_ok=True)
    plist_text = plistlib.dumps(plist_payload(repo_root), fmt=plistlib.FMT_XML).decode(
        "utf-8"
    )
    atomic_write_text(target, plist_text)
    _run_launchctl("bootout", launch_domain(), str(target), check=False)
    _run_launchctl("bootstrap", launch_domain(), str(target))
    _run_launchctl("enable", f"{launch_domain()}/{LABEL}")
    _run_launchctl("kickstart", "-k", f"{launch_domain()}/{LABEL}")


def start() -> None:
    target = launch_agent_path()
    if not target.is_file():
        raise RuntimeError("scheduler is not installed")
    _run_launchctl("enable", f"{launch_domain()}/{LABEL}")
    _run_launchctl("kickstart", "-k", f"{launch_domain()}/{LABEL}")


def stop() -> None:
    target = launch_agent_path()
    if target.is_file():
        _run_launchctl("bootout", launch_domain(), str(target), check=False)


def status() -> int:
    completed = _run_launchctl("print", f"{launch_domain()}/{LABEL}", check=False)
    if completed.stdout:
        print(completed.stdout.rstrip())
    if completed.stderr:
        print(completed.stderr.rstrip(), file=sys.stderr)
    return completed.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "start", "stop", "status"))
    args = parser.parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    try:
        if args.action == "install":
            install(repo_root)
            print(f"installed and started {LABEL}")
            return 0
        if args.action == "start":
            start()
            print(f"started {LABEL}")
            return 0
        if args.action == "stop":
            stop()
            print(f"stopped {LABEL}")
            return 0
        return status()
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        detail = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) and exc.stderr else str(exc)
        print(f"scheduler action failed: {detail}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
