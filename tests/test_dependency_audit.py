"""Run the actual audit entrypoint against controlled HTTP and uv failures.

The test-only Requests transport redirects prepared PyPI requests to a local
server. pip-audit and the production adapter are never patched. Real HTTP body
framing and the pinned native parser execute in a fresh subprocess.
"""

import importlib.util
import json
import os
import runpy
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import tomllib
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast
from unittest.mock import patch
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / ".ci" / "audit_dependencies.py"
AUDIT_PYTHON = os.environ.get("SOVTRACE_AUDIT_PYTHON") or (
    sys.executable if importlib.util.find_spec("pip_audit") else None
)
FUNCTIONS = runpy.run_path(str(ENTRYPOINT))


VALID_TIMESTAMPS = [
    None,
    "2026-01-01T00:00:00Z",
    *["2026-01-01T00:00:00." + "123456"[:digits] + "Z" for digits in range(1, 7)],
    "2024-02-29T23:59:59Z",
]
INVALID_TIMESTAMPS = [
    "20260101T000000Z",
    "2026-W01-1T00:00:00Z",
    "2026-001T00:00:00Z",
    "2026-01-01T00:00:00+02:00",
    "2026-01-01T00:00:00+00:00",
    "2026-01-01 00:00:00Z",
    "2026-01-01T00:00:00",
    "2026-02-30T00:00:00Z",
    "2025-02-29T00:00:00Z",
    "2026-01-01T25:00:00Z",
    "2026-01-01T00:60:00Z",
    "2026-01-01T00:00:60Z",
    "2026-01-01T00:00:00.1234567Z",
    "2026-01-01T00:00:00.Z",
    "2026-01-01t00:00:00Z",
    "2026-01-01T00:00:00z",
    "2026-01-01T00:00:00UTC",
    "2026-01-01T00:00:00+0000",
    "2026-01-01T00:00:00Z\n",
    "0000-01-01T00:00:00Z",
    "026-01-01T00:00:00Z",
    "02026-01-01T00:00:00Z",
    "2026-1-01T00:00:00Z",
    "2026-01-1T00:00:00Z",
    "2026-01-01T0:00:00Z",
    "2026-01-01T00:0:00Z",
    "2026-01-01T00:00:0Z",
    "2026-01-01T００:00:00Z",
    "",
    True,
    12,
    {},
]


class LocalPyPI(ThreadingHTTPServer):
    mode = "clean"
    events: list[tuple[str, ...]]
    project: Path
    advisory_timestamps: dict


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        server = cast(LocalPyPI, self.server)
        parts = urlsplit(self.path).path.strip("/").split("/")
        kind, name = parts[:2]
        version = parts[2] if kind == "pypi" else None
        server.events.append(tuple(parts))
        fault = name == "typing-inspection"  # Last of the current 29 selected pairs.
        mode = server.mode if fault else "clean"
        if kind == "pypi":
            data = {"info": {"name": name, "version": version}, "vulnerabilities": []}
            media = "application/json"
            if server.mode in {"vulnerable", "timestamp-advisory"} and name == "openai":
                data["vulnerabilities"] = [
                    {
                        "id": "SOVTRACE-CONTROL-0001",
                        "aliases": [],
                        "fixed_in": [],
                        "summary": "Synthetic control only",
                        **server.advisory_timestamps,
                    }
                ]
            if mode == "empty-object":
                data = {}
            elif mode == "missing-vulnerabilities":
                del data["vulnerabilities"]
            elif mode == "null-vulnerabilities":
                data["vulnerabilities"] = None
            elif mode == "wrong-vulnerability-type":
                data["vulnerabilities"] = {}
            elif mode == "wrong-identity":
                data["info"]["name"] = "different-package"
            elif mode == "wrong-version":
                data["info"]["version"] = "999"
            elif mode == "invalid-advisory":
                data["vulnerabilities"] = [{"id": "CONTROL", "aliases": None, "fixed_in": []}]
            elif mode == "invalid-withdrawal":
                data["vulnerabilities"] = [
                    {"id": "CONTROL", "aliases": [], "fixed_in": [], "withdrawn": "not-a-timestamp"}
                ]
            elif mode == "unfinished-page":
                data["next_page_token"] = "unfinished"
            elif mode == "mutate-input":
                with (server.project / "pyproject.toml").open("a") as file:
                    file.write("\n# changed during audit\n")
            elif mode == "delete-input":
                (server.project / "uv.lock").unlink()
        else:
            media = "application/vnd.pypi.simple.v1+json"
            data = {
                "name": name,
                "meta": {"api-version": "1.4"},
                "files": [],
                "project-status": {"status": "active"},
            }
            if mode in {"archived", "deprecated", "quarantined"}:
                data["project-status"]["status"] = mode
            elif mode == "unknown-status":
                data["project-status"]["status"] = "unknown"
            elif mode == "status-default":
                del data["project-status"]
            elif mode == "status-missing-identity":
                del data["name"]
            elif mode == "status-missing-meta":
                del data["meta"]
            elif mode == "status-wrong-version":
                data["meta"]["api-version"] = "2.0"
            elif mode == "status-null":
                data["project-status"] = None
        if (kind == "pypi" and mode == "disconnect") or (
            kind == "simple" and mode == "status-disconnect"
        ):
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            return
        code = (
            503
            if (kind, mode) in {("pypi", "unavailable"), ("simple", "status-unavailable")}
            else 200
        )
        if (kind, mode) in {("pypi", "missing-release"), ("simple", "status-missing")}:
            code = 404
        body = json.dumps(data).encode()
        if (kind, mode) in {("pypi", "malformed"), ("simple", "status-malformed")}:
            body = b"invalid-json"
        if kind == "pypi" and mode == "empty-body":
            body = b""
        if kind == "pypi" and mode == "duplicate-key":
            body = b'{"vulnerabilities": [], "vulnerabilities": []}'
        self.send_response(code)
        self.send_header("Content-Type", media)
        # Even syntactically complete JSON with incomplete HTTP framing must fail.
        length = len(body) + (10 if kind == "pypi" and mode == "truncated" else 0)
        self.send_header("Content-Length", str(length))
        self.end_headers()
        self.wfile.write(body)


class BasicControls(unittest.TestCase):
    def test_duplicate_json_keys_fail(self):
        with self.assertRaises(FUNCTIONS["AuditError"]):
            json.loads('{"key": 1, "key": 2}', object_pairs_hook=FUNCTIONS["unique_object"])

    def test_valid_utc_timestamps(self):
        for value in VALID_TIMESTAMPS:
            with self.subTest(value=value):
                self.assertIsNone(FUNCTIONS["timestamp"](value))

    def test_invalid_timestamp_cannot_withdraw_an_advisory(self):
        for value in INVALID_TIMESTAMPS:
            with self.subTest(value=value), self.assertRaises(FUNCTIONS["AuditError"]):
                FUNCTIONS["timestamp"](value)


@unittest.skipUnless(
    AUDIT_PYTHON and not os.environ.get("UV_OFFLINE"),
    "integrated HTTP controls run with the isolated audit environment",
)
class IntegratedAuditControls(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sovtrace-audit-control-")
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        for name in ("pyproject.toml", "uv.lock", ".python-version"):
            shutil.copyfile(ROOT / name, self.project / name)
        self.original = {n: (self.project / n).read_bytes() for n in ("pyproject.toml", "uv.lock")}
        self.server = LocalPyPI(("127.0.0.1", 0), Handler)
        self.server.events = []
        self.server.project = self.project
        self.server.advisory_timestamps = {}
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def run_audit(self, mode="clean", extra_env=None):
        self.server.mode = mode
        self.server.events.clear()
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("UV_", "OPENAI_")) and k != "PYTHONPATH"
        }
        env.update(UV_OFFLINE="1", UV_PYTHON=str(AUDIT_PYTHON), UV_PYTHON_DOWNLOADS="never")
        if extra_env:
            env.update(extra_env)
        result = subprocess.run(
            [
                str(AUDIT_PYTHON),
                str(Path(__file__).resolve()),
                "--audit-child",
                f"http://127.0.0.1:{self.server.server_port}",
                str(self.project),
                mode,
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result

    def assert_failed(self, result):
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn('"status": "passed"', result.stdout)

    def test_clean_exact_inventory_and_immutable_lock(self):
        result = self.run_audit()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        packages = tomllib.loads(self.original["uv.lock"].decode())["package"]
        registry = {(p["name"], p["version"]) for p in packages if "registry" in p["source"]}
        # Independent oracle for this project's sole conditional dependency.
        expected = registry - ({("colorama", "0.4.6")} if sys.platform != "win32" else set())
        actual = {(p["name"], p["version"]) for p in report["vulnerability_checks"]}
        requests = {(p[1], p[2]) for p in self.server.events if p[0] == "pypi"}
        self.assertEqual(actual, expected)
        self.assertEqual(requests, expected)
        self.assertEqual({p["name"] for p in report["status_checks"]}, {n for n, _ in expected})
        self.assertEqual(report["locked_registry_packages"], 30)
        self.assertEqual(report["local_projects"], [{"name": "sovtrace", "version": "0.1.0"}])
        self.assertEqual(len(actual), 30 if sys.platform == "win32" else 29)
        self.assertIn(("openai", "2.54.0"), actual)
        self.assertIn(("pytest", "9.1.1"), actual)
        self.assertIn(("nodejs-wheel-binaries", "24.19.0"), actual)
        for name, data in self.original.items():
            self.assertEqual((self.project / name).read_bytes(), data)

    def test_vulnerability_fails_with_useful_diagnostics(self):
        result = self.run_audit("vulnerable")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["status"], "blocked")
        self.assertIn(
            {"name": "openai", "version": "2.54.0", "id": "SOVTRACE-CONTROL-0001", "aliases": []},
            report["vulnerabilities"],
        )

    def test_valid_published_timestamps_match_native_parser_and_preserve_findings(self):
        for value in VALID_TIMESTAMPS:
            with self.subTest(published=value):
                self.server.advisory_timestamps = {"published": value, "withdrawn": None}
                result = self.run_audit("timestamp-advisory")
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                report = json.loads(result.stdout)
                self.assertEqual(report["status"], "blocked")
                self.assertEqual(
                    report["vulnerabilities"],
                    [
                        {
                            "name": "openai",
                            "version": "2.54.0",
                            "id": "SOVTRACE-CONTROL-0001",
                            "aliases": [],
                        }
                    ],
                )

    def test_valid_withdrawals_follow_pinned_policy(self):
        for value in VALID_TIMESTAMPS:
            if value is None:
                continue  # Null withdrawal remains an active finding, tested above.
            with self.subTest(withdrawn=value):
                self.server.advisory_timestamps = {
                    "published": "2024-01-01T00:00:00Z",
                    "withdrawn": value,
                }
                result = self.run_audit("timestamp-advisory")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                report = json.loads(result.stdout)
                self.assertEqual(report["status"], "passed")
                self.assertEqual(report["vulnerabilities"], [])
                self.assertIn(
                    {"name": "openai", "version": "2.54.0"}, report["vulnerability_checks"]
                )

    def test_malformed_advisory_timestamps_fail_production_entrypoint(self):
        for field in ["published", "withdrawn"]:
            for value in INVALID_TIMESTAMPS:
                with self.subTest(field=field, value=value):
                    # A valid non-null withdrawal makes the native service skip
                    # published parsing. The adapter must still reject invalid
                    # published data before it can disappear with that advisory.
                    self.server.advisory_timestamps = {
                        "published": "2024-01-01T00:00:00Z",
                        "withdrawn": "2026-01-01T00:00:00Z",
                        field: value,
                    }
                    result = self.run_audit("timestamp-advisory")
                    self.assert_failed(result)
                    self.assertIn("invalid advisory timestamp", result.stderr)

    def test_incomplete_or_invalid_vulnerability_checks_fail_integrated_command(self):
        for mode in [
            "empty-object",
            "empty-body",
            "missing-vulnerabilities",
            "null-vulnerabilities",
            "wrong-vulnerability-type",
            "wrong-identity",
            "wrong-version",
            "invalid-advisory",
            "invalid-withdrawal",
            "unfinished-page",
            "duplicate-key",
            "malformed",
            "unavailable",
            "missing-release",
            "disconnect",
            "truncated",
            "crash",
        ]:
            with self.subTest(mode=mode):
                result = self.run_audit(mode)
                self.assert_failed(result)
                if mode != "crash":
                    # Failure is at the final lookup, after the preceding 28 completed.
                    self.assertEqual(
                        len([p for p in self.server.events if p[0] == "pypi"]),
                        30 if sys.platform == "win32" else 29,
                    )

    def test_status_lookup_failures_and_invalid_payloads_fail_integrated_command(self):
        for mode in [
            "status-unavailable",
            "status-missing",
            "status-disconnect",
            "status-malformed",
            "status-missing-identity",
            "status-missing-meta",
            "status-wrong-version",
            "status-null",
            "unknown-status",
        ]:
            with self.subTest(mode=mode):
                self.assert_failed(self.run_audit(mode))

    def test_adverse_status_only_findings_fail(self):
        for status in ["archived", "deprecated", "quarantined"]:
            with self.subTest(status=status):
                result = self.run_audit(status)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                report = json.loads(result.stdout)
                self.assertEqual(report["vulnerabilities"], [])
                self.assertEqual(
                    report["adverse_statuses"],
                    [{"name": "typing-inspection", "versions": ["0.4.4"], "status": status}],
                )

    def test_specified_active_default_requires_a_valid_response(self):
        result = self.run_audit("status-default")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_stale_lock_fails_before_requests(self):
        path = self.project / "pyproject.toml"
        path.write_text(path.read_text().replace('"pytest",', '"pytest>=9",'))
        self.assert_failed(self.run_audit())
        self.assertEqual(self.server.events, [])
        self.assertEqual((self.project / "uv.lock").read_bytes(), self.original["uv.lock"])

    def test_absent_lock_fails_without_creating_one(self):
        (self.project / "uv.lock").unlink()
        self.assert_failed(self.run_audit())
        self.assertFalse((self.project / "uv.lock").exists())
        self.assertEqual(self.server.events, [])

    def test_narrowed_export_cannot_omit_runtime_or_development_packages(self):
        for mode in ["omit-runtime", "omit-development", "export-failure"]:
            with self.subTest(mode=mode):
                result = self.run_audit(mode)
                self.assert_failed(result)
                self.assertEqual(self.server.events, [])
                if mode == "export-failure":
                    self.assertEqual(result.returncode, 7)

    def test_unapproved_sources_and_selection_overrides_fail(self):
        path = self.project / "uv.lock"
        path.write_text(
            path.read_text().replace(
                'registry = "https://pypi.org/simple"',
                'registry = "https://example.invalid/simple"',
                1,
            )
        )
        self.assert_failed(self.run_audit())
        self.assertEqual(self.server.events, [])
        path.write_bytes(self.original["uv.lock"])
        for key in ["UV_FROZEN", "UV_PROJECT", "UV_WORKING_DIR", "UV_CONFIG_FILE", "UV_NO_DEV"]:
            with self.subTest(key=key):
                self.assert_failed(self.run_audit(extra_env={key: "1"}))
                self.assertEqual(self.server.events, [])

    def test_input_mutation_or_deletion_never_emits_a_clean_report(self):
        for mode in ["mutate-input", "delete-input"]:
            with self.subTest(mode=mode):
                for name, data in self.original.items():
                    (self.project / name).write_bytes(data)
                self.assert_failed(self.run_audit(mode))


def audit_child():
    HTTPAdapter = importlib.import_module("requests.adapters").HTTPAdapter

    origin, project, mode = sys.argv[2:]
    send, run = HTTPAdapter.send, subprocess.run

    def forward(self, request, **kwargs):
        if mode == "crash":
            raise RuntimeError("synthetic auditor transport crash")
        assert request.url.startswith("https://pypi.org/")
        assert kwargs.get("verify") is not False
        copied = request.copy()
        copied.url = origin + urlsplit(request.url).path
        kwargs["proxies"] = {}  # Local fixture only; no proxy or TLS interception.
        return send(self, copied, **kwargs)

    def export_control(args, **kwargs):
        result = run(args, **{**kwargs, "check": False})
        if args[:2] == ["uv", "export"]:
            if mode == "export-failure":
                result = subprocess.CompletedProcess(args, 7, "", "synthetic export failure")
            elif mode in {"omit-runtime", "omit-development"}:
                omitted = "openai==" if mode == "omit-runtime" else "pytest=="
                result.stdout = "\n".join(
                    line for line in result.stdout.splitlines() if not line.startswith(omitted)
                )
        if kwargs.get("check") and result.returncode:
            raise subprocess.CalledProcessError(
                result.returncode, args, result.stdout, result.stderr
            )
        return result

    sys.argv = [str(ENTRYPOINT), "--project", project]
    with patch.object(HTTPAdapter, "send", forward), patch("subprocess.run", export_control):
        runpy.run_path(str(ENTRYPOINT), run_name="__main__")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--audit-child":
        audit_child()
    else:
        unittest.main()
