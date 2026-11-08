# Contributing to SOVTrace

## Project scope

SOVTrace is deliberately limited to supported insurance Schedule of Values
workbooks and a bounded set of existence, count, TIV sum and asset-list operations.
Contributions should preserve that scope, its safety restrictions, exact monetary
handling and source-evidence guarantees. General spreadsheet interpretation,
general-purpose text-to-SQL, hosted services and additional providers are not
committed project features.

## Development setup and verification

Use Python 3.12 and [uv](https://docs.astral.sh/uv/getting-started/installation/).
From a local checkout, install the locked dependencies and run the existing checks:

```sh
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pyright
uv run --locked pytest -q
uv run --locked python scripts/demo.py
uv run --locked python scripts/demo.py --format json
uv run --locked python scripts/verify_synthetic.py --offline
```

The demonstrations and synthetic evaluator use artificial data and do not require
provider credentials.

## Security verification

Use **Gitleaks 8.30.1** and **uv 0.12.7**, matching CI. Install uv using the
[official installation instructions](https://docs.astral.sh/uv/getting-started/installation/)
and select [release 0.12.7](https://github.com/astral-sh/uv/releases/tag/0.12.7).
Download the archive for your OS and architecture from the
[official Gitleaks 8.30.1 release](https://github.com/gitleaks/gitleaks/releases/tag/v8.30.1).
Verify its SHA-256 against that release's `gitleaks_8.30.1_checksums.txt` **before**
extracting or executing it. On Linux use `sha256sum --check`; on macOS use
`shasum -a 256 -c` with the matching checksum entry; on Windows compare
`Get-FileHash -Algorithm SHA256` with that entry. Extract the verified executable
into a local directory on `PATH`, then confirm `gitleaks version` prints `8.30.1`
and `uv --version` reports `0.12.7`. CI verifies the Linux x64 archive against
SHA-256 `551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb`.

From a full checkout, fetch all destination branches and tags and run:

```sh
git fetch origin '+refs/heads/*:refs/remotes/origin/*' --tags
python3 .ci/scan_secrets.py .
uv audit --locked
```

A shallow clone must first be unshallowed (`git fetch --unshallow origin`). To
include an unmerged PR locally, fetch its head too, for example
`git fetch origin refs/pull/123/head:refs/remotes/origin/pr/123` (replace `123`).
CI fetches full history, including the PR merge commit and its reachable head;
`--all` scans every fetched reachable ref, rather than just the latest diff.
The wrapper executes `gitleaks git --redact --log-opts="--all" .` with fixed
console logging flags. It independently checks non-shallow, nonempty, readable Git
history, propagates Git and scanner errors, and requires recognised positive
commit-count and clean completion telemetry. Gitleaks 8.30.1 can otherwise return
zero after Git fails and zero commits are scanned. Missing, changed, zero-commit,
warning/error or partial-scan telemetry therefore fails verification. The count
reports commits with scanned fragments, so it need not equal Git's total count
(which can include empty or merge commits). Telemetry is a scanner completion
check, not a separate proof that Gitleaks detects every possible secret.

Run the scanner controls with the verified executable available on `PATH`:

```sh
GITLEAKS_BINARY="$(command -v gitleaks)" python3 -m unittest discover -s tests -p test_secret_scan.py -v
```

They generate nonfunctional credentials only in disposable repositories, cover
current/historical/ref-only findings and the original Git-error/exit-zero defect,
and also run in the security job. Ordinary offline pytest runs the wrapper's
simulated failure controls and skips real-scanner cases when no binary is supplied.
Use Gitleaks' default rules and keep full redaction enabled. Do not upload
unredacted reports or secret-bearing artifacts.

`uv audit --locked` checks the committed, current `uv.lock`, including direct and
transitive runtime and development dependencies. It must not change dependency
versions or rewrite the lockfile; check with
`git diff --exit-code -- pyproject.toml uv.lock`. Do not narrow dependency groups,
ignore advisories, or substitute `--frozen` for the current-lock requirement.
Dependency installation and auditing require network access; the audit uses uv's
default official OSV advisory service. Run it outside `UV_OFFLINE=1` and the Python
offline guard. The offline demo and synthetic evaluator need no provider
credentials or live model calls.

The separate security workflow runs on every PR and push to `main`. Its `secrets`
and `dependencies` jobs must both pass: findings, stale or absent locking,
scanner/setup errors, and unavailable or incomplete advisory-service responses
fail verification. Dependency diagnostics retain package versions and advisory
IDs. A service outage is a failure, not an advisory exception. These are CI checks;
this change does not configure branch protection or rulesets to prevent merging.
The existing offline workflow remains independent of advisory-service availability.
Its `PYTHONPATH`-loaded Python audit hook protects the intended Python verification
processes; it is not operating-system-wide network isolation.

For a real secret, stop affected work and report a sanitised finding privately to
the owner. Do not reproduce sensitive content, rewrite history, or rotate real
credentials without further authorisation. A false-positive exception needs the
smallest practical rule/path/finding scope, a documented rationale, explicit owner
approval and a review date; no baseline or blanket allowlist is permitted.

For vulnerable dependencies, prefer a bounded update. Stop for an owner decision
if remediation changes constraints, application behaviour or the agreed scope.
No advisories are ignored by default. Any temporary exception requires explicit
owner approval recording the package/version, advisory ID and aliases, runtime or
development scope, actual applicability/exposure, fixed versions and remediation
obstacle, mitigation, review date, and expiry or reassessment condition. Suppress
only the individually approved advisory IDs. This issue authorises no exceptions,
repository visibility changes, releases, tags or package distribution.

## Issues, pull requests and support

Once the repository is public, use GitHub issues for ordinary bug reports,
questions and feature proposals. Propose code changes through GitHub pull requests.
Include concise reproduction steps and applicable synthetic test coverage. Do not
submit real customer workbooks, secrets, or restricted personal or business
information in issues, pull requests, fixtures or other contributions.

Report security vulnerabilities according to [SECURITY.md](SECURITY.md), rather
than through ordinary public issues. Contributions are reviewed on a best-effort
basis; no response-time commitment is offered.
