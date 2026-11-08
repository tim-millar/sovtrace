"""Verify locked PyPI dependencies using pip-audit 2.10.1 and explicit completion ledgers."""

import argparse
import hashlib
import importlib.metadata
import json
import os
import subprocess
import sys
import tempfile
import tomllib
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit


class AuditError(Exception):
    """A required audit check could not be established."""


def require(condition, message):
    if not condition:
        raise AuditError(message)


def unique_object(pairs):
    obj = {}
    for key, value in pairs:
        require(key not in obj, "duplicate JSON key")
        obj[key] = value
    return obj


def timestamp(value):
    if value is not None:
        require(isinstance(value, str), "invalid advisory timestamp")
        require(
            datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is not None,
            "advisory timestamp has no timezone",
        )


def snapshot(root):
    return {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in ("pyproject.toml", "uv.lock")
    }


def command(args, root):
    return subprocess.run(args, cwd=root, check=True, capture_output=True, text=True).stdout


def inventory(root):
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name
    from packaging.version import Version

    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    lock = tomllib.loads((root / "uv.lock").read_text())
    require(lock.get("version") == 1, "unrecognised lockfile format")
    registry, local = set(), []
    for package in lock["package"]:
        pair = (canonicalize_name(package["name"]), str(Version(package["version"])))
        source = package["source"]
        if source == {"registry": "https://pypi.org/simple"}:
            require(pair not in registry, "duplicate locked dependency")
            registry.add(pair)
        else:
            require(
                source in ({"editable": "."}, {"virtual": "."})
                and pair[0] == canonicalize_name(project["name"]),
                "unsupported non-PyPI dependency source",
            )
            local.append({"name": pair[0], "version": pair[1]})
    require(registry and len(local) == 1, "missing registry inventory or local project")
    exported = command(
        [
            "uv",
            "export",
            "--project",
            str(root),
            "--locked",
            "--all-groups",
            "--all-extras",
            "--no-emit-project",
            "--no-hashes",
            "--no-header",
            "--no-annotate",
            "--format",
            "requirements.txt",
        ],
        root,
    )
    all_pairs, selected, inactive = set(), set(), []
    for line in exported.splitlines():
        if not line.strip():
            continue
        req = Requirement(line)
        specs = list(req.specifier)
        require(
            not req.url
            and not req.extras
            and len(specs) == 1
            and specs[0].operator == "=="
            and "*" not in specs[0].version,
            "export contains an unsupported or unpinned requirement",
        )
        pair = (canonicalize_name(req.name), str(Version(specs[0].version)))
        require(pair not in all_pairs, "duplicate exported requirement")
        all_pairs.add(pair)
        if req.marker is None or req.marker.evaluate():
            selected.add(pair)
        else:
            inactive.append(str(req))
    require(all_pairs == registry, "export does not match every locked registry name/version")
    require(selected, "empty selected dependency inventory")
    return selected, {
        "locked_registry_packages": len(registry),
        "local_projects": local,
        "inactive_requirements": inactive,
    }


def audit(selected):
    # These version-pinned library entry points are used without changing their
    # implementations. HTTPAdapter/Session.mount are Requests' transport API.
    import requests
    from packaging.utils import canonicalize_name
    from packaging.version import Version
    from pip_audit._service import PyPIService
    from pip_audit._service.interface import ResolvedDependency, VulnerabilityResult
    from requests.adapters import HTTPAdapter

    names = {name for name, _ in selected}

    class VerifiedResponses(HTTPAdapter):
        def __init__(self):
            super().__init__(max_retries=0)
            self.releases = {}
            self.statuses = {}

        def send(self, request, **kwargs):
            url = urlsplit(request.url)
            require(
                url.scheme == "https"
                and url.netloc == "pypi.org"
                and not url.query
                and not url.fragment
                and request.method == "GET",
                "unexpected audit request",
            )
            parts = url.path.strip("/").split("/")
            release = len(parts) == 4 and parts[0] == "pypi" and parts[3] == "json"
            if release:
                key = (parts[1], parts[2])
                require(
                    key in selected and key not in self.releases,
                    "unexpected or duplicate release lookup",
                )
            else:
                require(
                    len(parts) == 2
                    and parts[0] == "simple"
                    and parts[1] in names
                    and parts[1] not in self.statuses,
                    "unexpected or duplicate status lookup",
                )
                key = parts[1]
            require(kwargs.get("verify") is not False, "TLS verification must remain enabled")
            try:
                response = super().send(request, **kwargs)
                body = response.content  # urllib3 rejects incomplete HTTP framing.
            except requests.RequestException as error:
                raise AuditError(
                    f"incomplete PyPI lookup for {key}: {type(error).__name__}"
                ) from None
            require(response.status_code == 200, f"PyPI lookup failed for {key}")
            expected_media = (
                "application/json" if release else "application/vnd.pypi.simple.v1+json"
            )
            require(
                response.headers.get("Content-Type", "").split(";")[0] == expected_media,
                f"unrecognised response media type for {key}",
            )
            require(len(body) <= 32 * 1024 * 1024, "audit response exceeds 32 MiB")
            data = json.loads(
                body,
                object_pairs_hook=unique_object,
                parse_constant=lambda _: require(False, "invalid JSON constant"),
            )
            require(isinstance(data, dict), f"invalid PyPI response for {key}")
            require(
                not {"next_page_token", "nextPageToken"} & data.keys(),
                "unexpected pagination in a non-paginated PyPI response",
            )
            if release:
                info = data.get("info")
                require(
                    isinstance(info, dict)
                    and isinstance(info.get("name"), str)
                    and isinstance(info.get("version"), str),
                    f"missing release identity for {key}",
                )
                require(
                    (canonicalize_name(info["name"]), str(Version(info["version"]))) == key,
                    f"release identity mismatch for {key}",
                )
                vulns = data.get("vulnerabilities")
                require(isinstance(vulns, list), f"missing vulnerability collection for {key}")
                active = set()
                for vuln in vulns:
                    require(
                        isinstance(vuln, dict)
                        and isinstance(vuln.get("id"), str)
                        and bool(vuln["id"]),
                        f"invalid advisory for {key}",
                    )
                    for field in ("aliases", "fixed_in"):
                        require(
                            isinstance(vuln.get(field), list)
                            and all(isinstance(v, str) and v for v in vuln[field]),
                            f"invalid advisory {field} for {key}",
                        )
                    for fixed in vuln["fixed_in"]:
                        Version(fixed)
                    for field in ("summary", "details"):
                        require(
                            vuln.get(field) is None or isinstance(vuln[field], str),
                            f"invalid advisory {field} for {key}",
                        )
                    timestamp(vuln.get("published"))
                    timestamp(vuln.get("withdrawn"))
                    if vuln.get("withdrawn") is None:
                        active.add(vuln["id"])
                self.releases[key] = active
            else:
                require(
                    data.get("name") == key
                    and isinstance(data.get("files"), list)
                    and isinstance(data.get("meta"), dict)
                    and data["meta"].get("api-version") == "1.4",
                    f"invalid status response for {key}",
                )
                marker = data.get("project-status", {})
                require(
                    isinstance(marker, dict) and set(marker) <= {"status", "reason"},
                    f"invalid status marker for {key}",
                )
                status = marker.get("status", "active")  # Specified default, after a valid lookup.
                require(
                    status in {"active", "archived", "deprecated", "quarantined"}
                    and ("reason" not in marker or isinstance(marker["reason"], str)),
                    f"unrecognised project status for {key}",
                )
                self.statuses[key] = status
            return response

    adapter = VerifiedResponses()
    findings, completed = [], set()
    with tempfile.TemporaryDirectory(prefix="sovtrace-audit-cache-") as cache:
        service = PyPIService(cache_dir=Path(cache), timeout=10)
        # A more-specific uncached adapter supersedes the service's general cache
        # adapter. No persistent response can hide a failed lookup on this run.
        service.session.mount("https://pypi.org/", adapter)
        try:
            for name, version in sorted(selected):
                pair = (name, version)
                dep, vulns = service.query(ResolvedDependency(name=name, version=Version(version)))
                require(
                    isinstance(dep, ResolvedDependency)
                    and (dep.canonical_name, str(dep.version)) == pair,
                    f"auditor skipped or changed {pair}",
                )
                require(
                    isinstance(vulns, list)
                    and all(isinstance(v, VulnerabilityResult) for v in vulns),
                    f"unusable auditor result for {pair}",
                )
                ids = {str(v.id) for v in vulns} | {str(a) for v in vulns for a in v.aliases}
                require(
                    pair in adapter.releases and adapter.releases[pair] <= ids,
                    f"auditor omitted a required check or advisory for {pair}",
                )
                completed.add(pair)
                findings.extend(
                    {
                        "name": name,
                        "version": version,
                        "id": str(v.id),
                        "aliases": sorted(str(a) for a in v.aliases),
                    }
                    for v in vulns
                )
            for name in sorted(names):
                service.session.get(
                    f"https://pypi.org/simple/{name}/",
                    headers={"Accept": "application/vnd.pypi.simple.v1+json"},
                    timeout=10,
                )
        finally:
            service.session.close()
    require(
        completed == selected
        and set(adapter.releases) == selected
        and set(adapter.statuses) == names,
        "incomplete audit completion ledger",
    )
    adverse = [
        {"name": name, "status": status, "versions": sorted(v for n, v in selected if n == name)}
        for name, status in sorted(adapter.statuses.items())
        if status != "active"
    ]
    return {
        "vulnerability_checks": [dict(name=n, version=v) for n, v in sorted(completed)],
        "status_checks": [{"name": n, "status": s} for n, s in sorted(adapter.statuses.items())],
        "vulnerabilities": findings,
        "adverse_statuses": adverse,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path.cwd())
    root = parser.parse_args().project.resolve()
    report, code = None, 2
    before = None
    try:
        before = snapshot(root)
        require(sys.version_info[:2] == (3, 12), "Python 3.12 is required")
        require(importlib.metadata.version("pip-audit") == "2.10.1", "pip-audit 2.10.1 is required")
        require(
            command(["uv", "--version"], root).split()[:2] == ["uv", "0.12.7"],
            "uv 0.12.7 is required",
        )
        require(
            not any(
                os.environ.get(k)
                for k in (
                    "UV_FROZEN",
                    "UV_PROJECT",
                    "UV_WORKING_DIR",
                    "UV_CONFIG_FILE",
                    "UV_NO_CONFIG",
                    "UV_NO_DEV",
                    "UV_NO_DEFAULT_GROUPS",
                    "UV_NO_GROUP",
                    "UV_ONLY_DEV",
                    "UV_ONLY_GROUP",
                    "UV_PYTHON_PLATFORM",
                    "UV_PYTHON_VERSION",
                )
            ),
            "audit selection overrides are not permitted",
        )
        selected, info = inventory(root)
        report = {**info, **audit(selected)}
        code = int(bool(report["vulnerabilities"] or report["adverse_statuses"]))
        report["status"] = "blocked" if code else "passed"
    except subprocess.CalledProcessError as error:
        code = error.returncode if error.returncode > 0 else 128 - error.returncode
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error": "uv command failed",
                    "diagnostic": error.stderr[:4000],
                }
            ),
            file=sys.stderr,
        )
    except Exception as error:
        message = str(error) if isinstance(error, AuditError) else type(error).__name__
        print(json.dumps({"status": "failed", "error": message}), file=sys.stderr)
    finally:
        try:
            unchanged = before is None or snapshot(root) == before
        except OSError:
            unchanged = False
        if not unchanged:
            report, code = None, 2
            print('{"status":"failed","error":"project or lockfile changed"}', file=sys.stderr)
    if report is not None:
        print(json.dumps(report, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
