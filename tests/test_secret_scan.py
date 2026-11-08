"""Wrapper regression tests; real-scanner controls use disposable Git repositories."""

import contextlib
import io
import os
import runpy
import secrets
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

WRAPPER = Path(__file__).resolve().parents[1] / ".ci" / "scan_secrets.py"
FUNCTIONS = runpy.run_path(str(WRAPPER))
GITLEAKS = os.environ.get("GITLEAKS_BINARY") or shutil.which("gitleaks")
CLEAN = (
    "10:00AM INF 1 commits scanned.\n"
    "10:00AM INF scanned ~24 bytes (24 bytes) in 1ms\n"
    "10:00AM INF no leaks found\n"
)


def completed(code: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess([], code, stdout, stderr)


def preflight():
    return [
        completed(stdout="false\n"),
        completed(stdout="1\n"),
        completed(),
        completed(stdout="8.30.1\n"),
    ]


class WrapperTests(unittest.TestCase):
    def scan(self, responses):
        with (
            patch("subprocess.run", side_effect=responses) as command,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            status = FUNCTIONS["scan"](Path("/synthetic"), "gitleaks")
        return status, command

    def test_clean_scan_checks_git_then_uses_full_history_and_redaction(self):
        status, command = self.scan([*preflight(), completed(stderr=CLEAN)])
        self.assertEqual(status, 0)
        self.assertEqual(command.call_args_list[1].args[0][-3:], ["rev-list", "--count", "--all"])
        self.assertEqual(command.call_args_list[2].args[0][-4:], ["log", "-p", "-U0", "--all"])
        self.assertEqual(command.call_args_list[2].kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(
            command.call_args.args[0],
            [
                "gitleaks",
                "git",
                "--redact",
                "--log-opts=--all",
                "--no-banner",
                "--no-color",
                "--log-level=info",
                "/synthetic",
            ],
        )

    def test_git_failures_propagate_without_starting_scanner(self):
        for index in range(3):
            with self.subTest(git_check=index):
                status, command = self.scan(
                    [*preflight()[:index], completed(128, stderr="fatal: Git error\n")]
                )
                self.assertEqual(status, 128)
                self.assertEqual(command.call_count, index + 1)

    def test_empty_or_unrecognised_git_history_fails(self):
        for count in ["0\n", "", "unknown\n"]:
            with self.subTest(count=count):
                status, command = self.scan([completed(stdout="false\n"), completed(stdout=count)])
                self.assertNotEqual(status, 0)
                self.assertEqual(command.call_count, 2)

    def test_shallow_or_unrecognised_checkout_fails(self):
        for shallow in ["true\n", "", "unknown\n"]:
            with self.subTest(shallow=shallow):
                status, _ = self.scan([completed(stdout=shallow)])
                self.assertNotEqual(status, 0)

    def test_scanner_nonzero_codes_propagate_including_detected_secrets(self):
        for code in [1, 2, 7, 126]:
            with self.subTest(code=code):
                status, _ = self.scan([*preflight(), completed(code, stderr=CLEAN)])
                self.assertEqual(status, code)

    def test_missing_unrecognised_zero_or_incomplete_telemetry_fails(self):
        for telemetry in [
            "",
            CLEAN.replace("1 commits scanned.", "0 commits scanned."),
            CLEAN.replace("1 commits scanned.", "one commits scanned."),
            CLEAN.replace("1 commits scanned.", "1 commit scanned."),
            CLEAN.replace("1 commits scanned.", "-1 commits scanned."),
            CLEAN.replace("10:00AM INF 1 commits scanned.\n", ""),
            CLEAN + "10:00AM INF 1 commits scanned.\n",
            CLEAN.replace("no leaks found", "no leaks found in partial scan"),
            CLEAN.replace("10:00AM INF no leaks found\n", ""),
            CLEAN.replace("10:00AM INF scanned ~24 bytes (24 bytes) in 1ms\n", ""),
            "10:00AM ERR [git] fatal: failure\n" + CLEAN,
            "10:00AM WRN partial scan completed\n" + CLEAN,
            CLEAN.replace("INF", "UNKNOWN"),
            "10:00AM INF unknown scanner notice\n" + CLEAN,
        ]:
            with self.subTest(telemetry=telemetry):
                status, _ = self.scan([*preflight(), completed(stderr=telemetry)])
                self.assertNotEqual(status, 0)

    def test_unrecognised_stdout_cannot_hide_a_scanner_error(self):
        status, _ = self.scan([*preflight(), completed(stdout="unexpected error", stderr=CLEAN)])
        self.assertNotEqual(status, 0)

    def test_wrong_scanner_version_fails(self):
        status, command = self.scan([*preflight()[:3], completed(stdout="8.30.0\n")])
        self.assertNotEqual(status, 0)
        self.assertEqual(command.call_count, 4)

    def test_version_command_failure_propagates(self):
        status, _ = self.scan([*preflight()[:3], completed(9)])
        self.assertEqual(status, 9)

    def test_missing_executable_fails_cli(self):
        with (
            patch.object(sys, "argv", [str(WRAPPER)]),
            patch("subprocess.run", side_effect=FileNotFoundError("missing executable")),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertNotEqual(FUNCTIONS["main"](), 0)


@unittest.skipUnless(GITLEAKS, "real Gitleaks controls run in the security job")
class RealScannerTests(unittest.TestCase):
    def setUp(self):
        assert GITLEAKS is not None
        self.binary = str(Path(GITLEAKS).resolve())
        self.temporary = tempfile.TemporaryDirectory(prefix="sovtrace-secret-control-")
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name)
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Synthetic scanner control")
        self.git("config", "user.email", "synthetic@example.invalid")
        (self.repo / "README.txt").write_text("Clean disposable repository.\n")
        self.commit("Clean control")
        version = self.run_command([self.binary, "version"])
        self.assertEqual(version.returncode, 0)
        self.assertEqual(version.stdout.strip(), "8.30.1")

    def run_command(self, args):
        return subprocess.run(args, cwd=self.repo, capture_output=True, text=True, timeout=30)

    def git(self, *args):
        result = self.run_command(["git", *args])
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def commit(self, message):
        self.git("add", ".")
        self.git("-c", "commit.gpgsign=false", "commit", "-m", message)

    def scan(self, binary=None):
        return self.run_command(
            [
                sys.executable,
                str(WRAPPER),
                "--gitleaks",
                binary or self.binary,
                str(self.repo),
            ]
        )

    def add_nonfunctional_credential(self):
        # Constructed at runtime; never stored in SOVTrace history or sent to a provider.
        sentinel = "gh" + "p_" + secrets.token_hex(18)
        (self.repo / "credential.txt").write_text("token=" + sentinel + "\n")
        self.commit("Disposable synthetic credential")
        return sentinel

    def test_legitimate_clean_scan(self):
        result = self.scan()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("1 commits scanned.", result.stderr)

    def test_current_and_historical_secret_detection_is_redacted(self):
        sentinel = self.add_nonfunctional_credential()
        for historical in [False, True]:
            if historical:
                self.git("rm", "credential.txt")
                self.commit("Remove disposable credential from working tree")
            with self.subTest(historical=historical):
                result = self.scan()
                self.assertEqual(result.returncode, 1)
                self.assertNotIn(sentinel, result.stdout + result.stderr)
                self.assertIn("leaks found", result.stderr)

    def test_secrets_on_other_reachable_refs_are_detected(self):
        self.git("switch", "-c", "side")
        sentinel = self.add_nonfunctional_credential()
        self.git("switch", "main")
        self.assertFalse((self.repo / "credential.txt").exists())
        result = self.scan()
        self.assertEqual(result.returncode, 1)
        self.assertNotIn(sentinel, result.stdout + result.stderr)

    def test_exact_git_error_exit_zero_defect_is_reproduced_and_rejected(self):
        raw = self.run_command(
            [
                self.binary,
                "git",
                "--redact",
                "--log-opts=--invalid-git-option",
                ".",
            ]
        )
        self.assertEqual(raw.returncode, 0)
        self.assertIn("unrecognized argument: --invalid-git-option", raw.stderr)
        self.assertIn("0 commits scanned.", raw.stderr)
        # Inject precisely that failure after the wrapper's independent Git checks.
        # The production wrapper never accepts caller-supplied Git scan options.
        shim = self.repo / "gitleaks-shim"
        shim.write_text(
            f"#!{sys.executable}\nimport os, sys\n"
            "args = sys.argv[1:]\n"
            "if args[0] == 'git':\n"
            "    args[args.index('--log-opts=--all')] = '--log-opts=--invalid-git-option'\n"
            f"os.execv({self.binary!r}, [{self.binary!r}, *args])\n"
        )
        shim.chmod(0o700)
        wrapped = self.scan(str(shim))
        self.assertNotEqual(wrapped.returncode, 0)
        self.assertIn("0 commits scanned.", wrapped.stderr)
        self.assertIn("Secret scan failed", wrapped.stderr)


if __name__ == "__main__":
    unittest.main()
