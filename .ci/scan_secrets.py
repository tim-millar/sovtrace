"""Fail-closed full-history scanning for the pinned Gitleaks 8.30.1 CLI."""

import argparse
import re
import subprocess
import sys
from pathlib import Path


def fail(message: str) -> int:
    print(f"Secret scan failed: {message}", file=sys.stderr)
    return 2


def scan(repository: Path, gitleaks: str) -> int:
    # rev-list verifies reachable commits; log also reads their patches. Discard
    # patch output so the independent Git check cannot expose source secrets.
    shallow = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "--is-shallow-repository"],
        capture_output=True,
        text=True,
    )
    sys.stderr.write(shallow.stderr)
    if shallow.returncode:
        return shallow.returncode
    if shallow.stdout.strip() != "false":
        return fail("a complete, non-shallow Git checkout is required")
    history = subprocess.run(
        ["git", "-C", str(repository), "rev-list", "--count", "--all"],
        capture_output=True,
        text=True,
    )
    sys.stderr.write(history.stderr)
    if history.returncode:
        return history.returncode
    if not re.fullmatch(r"[1-9][0-9]*\n?", history.stdout):
        return fail("Git history is empty or its commit count is unrecognised")
    patches = subprocess.run(
        ["git", "-C", str(repository), "log", "-p", "-U0", "--all"],
        stdout=subprocess.DEVNULL,
    )
    if patches.returncode:
        return patches.returncode

    version = subprocess.run([gitleaks, "version"], capture_output=True, text=True)
    sys.stderr.write(version.stderr)
    if version.returncode:
        return version.returncode
    if version.stdout.strip() != "8.30.1":
        return fail("Gitleaks 8.30.1 is required")
    result = subprocess.run(
        [
            gitleaks,
            "git",
            "--redact",
            "--log-opts=--all",
            "--no-banner",
            "--no-color",
            "--log-level=info",
            str(repository),
        ],
        capture_output=True,
        text=True,
    )
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    if result.returncode:
        return result.returncode

    # This pinned CLI has no structured completion telemetry. Require its known
    # console format, one positive commit count and final clean-scan summary.
    # Errors/warnings (including partial scans) must fail even with exit code 0.
    if result.stdout:
        return fail("unrecognised Gitleaks output on stdout")
    messages = []
    for line in result.stderr.splitlines():
        match = re.fullmatch(r"\S+ INF (.+)", line)
        if match is None:
            return fail("unrecognised or adverse Gitleaks telemetry")
        messages.append(match[1])
    if len(messages) != 3 or not re.fullmatch(r"[1-9][0-9]* commits scanned\.", messages[0]):
        return fail("missing, unrecognised or zero-commit scan telemetry")
    if not re.fullmatch(r"scanned ~[0-9]+ bytes \(.+\) in .+", messages[1]):
        return fail("missing or unrecognised scan completion telemetry")
    if messages[2] != "no leaks found":
        return fail("missing clean-scan completion telemetry")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository", nargs="?", type=Path, default=Path("."))
    parser.add_argument("--gitleaks", default="gitleaks", help="verified 8.30.1 executable")
    args = parser.parse_args()
    try:
        status = scan(args.repository.resolve(), args.gitleaks)
        return status if status >= 0 else 128 - status
    except OSError as error:
        return fail(str(error))


if __name__ == "__main__":
    raise SystemExit(main())
