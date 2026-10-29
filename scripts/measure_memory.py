"""Measure aggregate RSS/PSS for a command and every process it owns."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import time
from pathlib import Path

import psutil


def _sample(root: psutil.Process) -> tuple[int, int, list[dict[str, object]]]:
    processes = [root, *root.children(recursive=True)]
    rows: list[dict[str, object]] = []
    rss_total = 0
    pss_total = 0
    for process in processes:
        try:
            memory = process.memory_info()
            rss = int(memory.rss)
            try:
                pss = int(process.memory_full_info().pss)
            except (AttributeError, psutil.AccessDenied, psutil.NoSuchProcess):
                pss = 0
            rows.append(
                {
                    "pid": process.pid,
                    "rss_bytes": rss,
                    "pss_bytes": pss,
                    "command": process.cmdline(),
                }
            )
            rss_total += rss
            pss_total += pss
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
    return rss_total, pss_total, rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=10)
    parser.add_argument("--interval", type=float, default=0.1)
    parser.add_argument("--output", type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a command is required after --")

    child = subprocess.Popen(command, start_new_session=True)
    root = psutil.Process(child.pid)
    peak_rss = peak_pss = 0
    peak_rows: list[dict[str, object]] = []
    samples = 0
    started = time.monotonic()
    try:
        while child.poll() is None and time.monotonic() - started < args.seconds:
            rss, pss, rows = _sample(root)
            samples += 1
            if rss > peak_rss:
                peak_rss, peak_pss, peak_rows = rss, pss, rows
            time.sleep(args.interval)
    finally:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()

    report = {
        "command": command,
        "duration_seconds": round(time.monotonic() - started, 3),
        "samples": samples,
        "peak_rss_bytes": peak_rss,
        "peak_pss_bytes": peak_pss,
        "processes_at_rss_peak": peak_rows,
    }
    rendered = json.dumps(report, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
