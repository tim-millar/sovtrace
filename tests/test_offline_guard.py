"""Exercise the CI guard through Python startup, without external network access."""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

GUARD = Path(__file__).resolve().parents[1] / ".ci" / "offline_guard"


def run_guarded_python(code: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code), str(GUARD / "sitecustomize.py")],
        cwd=cwd,
        env={**os.environ, "PYTHONPATH": str(GUARD)},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def test_guard_loads_at_startup_and_allows_non_network_operations(tmp_path: Path) -> None:
    result = run_guarded_python(
        """
        import json
        import sys
        from decimal import Decimal
        from pathlib import Path

        assert Path(sys.modules["sitecustomize"].__file__).resolve() == Path(sys.argv[1])
        assert sum(range(10)) == 45
        assert Decimal("0.1") + Decimal("0.2") == Decimal("0.3")
        assert json.loads('{"value": 42}')["value"] == 42
        print("startup and ordinary operations passed")
        """,
        tmp_path,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "startup and ordinary operations passed"


@pytest.mark.parametrize(
    "operation",
    [
        "sock.connect(('127.0.0.1', 9))",
        "sock.connect_ex(('127.0.0.1', 9))",
        "socket.getaddrinfo('localhost', 9)",
        "sock.bind(('127.0.0.1', 0))",
    ],
    ids=["connect", "connect_ex", "getaddrinfo", "bind"],
)
def test_guard_rejects_socket_operations(tmp_path: Path, operation: str) -> None:
    # A later hook prevents real I/O even if the guard regresses. Audit hooks run
    # in registration order, so only the startup guard can supply the expected error.
    result = run_guarded_python(
        """
        import socket
        import sys
        from pathlib import Path

        assert Path(sys.modules["sitecustomize"].__file__).resolve() == Path(sys.argv[1])

        def tripwire(event, args):
            if event in {"socket.connect", "socket.connect_ex", "socket.getaddrinfo", "socket.bind"}:
                raise AssertionError("Startup guard failed to block network operation")

        sys.addaudithook(tripwire)
        with socket.socket() as sock:
            try:
                SOCKET_OPERATION
            except RuntimeError as error:
                assert str(error) == "Network access is prohibited during offline verification"
            else:
                raise AssertionError("Network operation was allowed")
        print("blocked")
        """.replace("SOCKET_OPERATION", operation),
        tmp_path,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "blocked"
