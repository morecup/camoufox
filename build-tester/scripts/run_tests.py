#!/usr/bin/env python3
"""
Camoufox Build Tester — Python CLI

Runs the same antibot-detection checks as the Next.js web app,
but as a standalone CLI with ASCII art certificate output.

Usage:
  python scripts/run_tests.py <binary_path> [options]

Options:
  --profile-count N     Number of profiles to test (1-8, default: 8)
  --secret KEY          HMAC signing key for certificate
  --save-cert PATH      Save certificate text to this file
  --no-cert             Skip certificate generation
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys

from runner import run_tests


def _configure_stdio() -> None:
    """Avoid crashing on legacy Windows console encodings during CLI output."""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(errors="replace")


def _cleanup_stale_windows_test_processes(binary_path: str) -> None:
    """Terminate stale headless Camoufox test processes for the same binary."""
    if sys.platform != "win32":
        return

    resolved_binary = os.path.normcase(os.path.abspath(binary_path))
    query = (
        "$ErrorActionPreference='Stop'; "
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -ieq 'camoufox.exe' } | "
        "Select-Object ProcessId, CommandLine | ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", query],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        print(f"Warning: failed to query stale Windows processes: {exc}", file=sys.stderr)
        return

    if result.returncode != 0 or not result.stdout.strip():
        if result.returncode != 0:
            print(
                "Warning: failed to query stale Windows processes: "
                f"{result.stderr.strip() or result.stdout.strip()}",
                file=sys.stderr,
            )
        return

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        print(f"Warning: failed to parse process query output: {exc}", file=sys.stderr)
        return

    processes = payload if isinstance(payload, list) else [payload]
    stale_pids = []
    for proc in processes:
        if not isinstance(proc, dict):
            continue
        pid = proc.get("ProcessId")
        command_line = proc.get("CommandLine") or ""
        if not isinstance(pid, int) or not command_line:
            continue
        normalized_command = os.path.normcase(command_line)
        if resolved_binary not in normalized_command:
            continue
        if not any(flag in normalized_command for flag in ("-headless", "-juggler-pipe", "-contentproc")):
            continue
        stale_pids.append(str(pid))

    if not stale_pids:
        return

    print(
        "Pre-run cleanup: terminating stale Camoufox test processes for this binary: "
        + ", ".join(stale_pids)
    )
    kill_command = (
        "$ErrorActionPreference='Stop'; "
        f"Stop-Process -Id {','.join(stale_pids)} -Force -ErrorAction SilentlyContinue; "
        "Start-Sleep -Milliseconds 500"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", kill_command],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        print(f"Warning: failed to terminate stale Windows processes: {exc}", file=sys.stderr)


def main():
    _configure_stdio()
    parser = argparse.ArgumentParser(
        description="Camoufox Build Tester — runs antibot-detection checks via Playwright",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("binary_path", help="Path to the Camoufox (Firefox) binary")
    parser.add_argument(
        "--profile-count", type=int, default=8, metavar="N",
        help="Number of profiles to test, 1-8 (default: 8)",
    )
    parser.add_argument(
        "--secret", default="camoufox-tester-dev-secret", metavar="KEY",
        help="HMAC signing key for the certificate (default: dev secret)",
    )
    parser.add_argument(
        "--save-cert", metavar="PATH",
        help="Save the ASCII certificate to this file",
    )
    parser.add_argument(
        "--no-cert", action="store_true",
        help="Skip certificate generation",
    )
    args = parser.parse_args()

    profile_count = max(1, min(8, args.profile_count))

    binary_path = args.binary_path
    # Resolve macOS .app bundle to internal binary
    if sys.platform == "darwin" and binary_path.endswith(".app"):
        candidate = os.path.join(binary_path, "Contents", "MacOS", "camoufox")
        if os.path.isfile(candidate):
            binary_path = candidate
        else:
            candidate2 = os.path.join(binary_path, "Contents", "MacOS", "firefox")
            if os.path.isfile(candidate2):
                binary_path = candidate2

    if not os.path.isfile(binary_path):
        print(f"ERROR: Binary not found: {binary_path}", file=sys.stderr)
        sys.exit(1)

    _cleanup_stale_windows_test_processes(binary_path)

    print(f"Camoufox Build Tester")
    print(f"Binary:   {binary_path}")
    print(f"Profiles: {profile_count}")

    exit_code = asyncio.run(
        run_tests(
            binary_path=binary_path,
            profile_count=profile_count,
            secret=args.secret,
            save_cert=args.save_cert,
            no_cert=args.no_cert,
        )
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
