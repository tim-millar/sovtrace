"""Offline smoke tests for the installed application and console entry point."""

import importlib
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest
from test_ingestion import saved_manifest
from test_schema_inference import EXPECTED, RENAMED, FakeInference, proposal
from workbook_factory import HEADERS, asset, write_workbook

from sovtrace import cli
from sovtrace.cli import main


@pytest.fixture(autouse=True)
def without_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


def test_imports() -> None:
    importlib.import_module("sovtrace")
    cli = importlib.import_module("sovtrace.cli")
    assert callable(cli.main)


@pytest.mark.parametrize(
    ("arguments", "exit_code", "expected"),
    [
        ([], 0, ""),
        (["--help"], 0, "usage: sovtrace"),
        (["--version"], 0, "sovtrace 0.1.0"),
        (["--unknown"], 2, "unrecognized arguments: --unknown"),
    ],
)
def test_cli(arguments: list[str], exit_code: int, expected: str) -> None:
    executable = shutil.which("sovtrace")
    assert executable is not None
    result = subprocess.run(
        [executable, *arguments], capture_output=True, text=True, timeout=10, check=False
    )
    assert result.returncode == exit_code
    if exit_code == 0:
        assert expected in result.stdout
        assert result.stderr == ""
    else:
        assert expected in result.stderr


@pytest.mark.parametrize(
    "flags,enabled", [([], True), (["--infer-schema"], True), (["--no-infer-schema"], False)]
)
@pytest.mark.parametrize("replace", [False, True])
def test_ingest_argument_forwarding(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    flags: list[str],
    enabled: bool,
    replace: bool,
) -> None:
    importer = Mock(return_value={"result": "unchanged"})
    monkeypatch.setattr(cli, "ingest_workbook", importer)
    cli.main(
        [
            "ingest",
            "--format",
            "json",
            "source.xlsx",
            "--db",
            "output.sqlite",
            *flags,
            *(["--replace"] if replace else []),
        ]
    )
    importer.assert_called_once_with(
        Path("source.xlsx"), Path("output.sqlite"), replace=replace, infer_schema=enabled
    )
    assert json.loads(capsys.readouterr().out) == {"result": "unchanged"}


@pytest.mark.parametrize(
    "arguments,message",
    [
        (["source.xlsx"], "required: --db"),
        (["--db", "out.sqlite"], "required: source"),
        (["source.xlsx", "--db"], "expected one argument"),
        (["source.xlsx", "--db", "out.sqlite", "--unknown"], "unrecognized arguments"),
        (
            ["source.xlsx", "--db", "out.sqlite", "--infer-schema", "--no-infer-schema"],
            "not allowed with argument",
        ),
        (
            ["source.xlsx", "--db", "out.sqlite", "--no-infer-schema", "--infer-schema"],
            "not allowed with argument",
        ),
    ],
)
def test_ingest_argument_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
    message: str,
) -> None:
    importer = Mock(side_effect=AssertionError("Parser must reject before ingestion"))
    monkeypatch.setattr(cli, "ingest_workbook", importer)
    with pytest.raises(SystemExit) as exc:
        cli.main(["ingest", "--format", "json", *arguments])
    assert exc.value.code == 2
    assert message in capsys.readouterr().err
    importer.assert_not_called()


@pytest.mark.parametrize("credentials", [False, True])
def test_known_cli_modes_preserve_data_without_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    credentials: bool,
) -> None:
    if credentials:
        monkeypatch.setenv("OPENAI_API_KEY", "synthetic-unused-key")
    constructor = Mock(side_effect=AssertionError("Must not construct provider"))
    monkeypatch.setattr("sovtrace.ingestion.provider.OpenAISchemaInferenceProvider", constructor)
    source = write_workbook(tmp_path / "known.xlsx", rows=[asset(), asset()])
    snapshots = []
    reports = []
    for index, flags in enumerate([[], ["--infer-schema"], ["--no-infer-schema"]]):
        db = tmp_path / f"known-{index}.sqlite"
        main(["ingest", "--format", "json", str(source), "--db", str(db), *flags])
        report = json.loads(capsys.readouterr().out)
        assert report.pop("schema_inference") == {
            "status": "disabled" if index == 2 else "not_needed",
            "model": None,
            "inferred_fields": [],
        }
        report.pop("output")
        reports.append(report)
        with sqlite3.connect(db) as c:
            snapshots.append(
                (
                    saved_manifest(db),
                    [
                        c.execute(f"SELECT * FROM {table} ORDER BY 1, 2, 3").fetchall()
                        for table in (
                            "assets",
                            "source_columns",
                            "source_rows",
                            "source_cells",
                            "asset_sources",
                            "diagnostics",
                            "field_completeness",
                        )
                    ],
                )
            )
            assert c.execute(
                "SELECT asset_id, state, total_insurable_value_micro_usd FROM assets"
            ).fetchall() == [("TEST-1", "AZ", 10000000000001)]
        assert report["physical_records"] == 2
        assert report["verified_duplicate_groups"] == 1
        assert {f: m["column"] for f, m in saved_manifest(db)["concepts"].items()} == EXPECTED
    assert snapshots[0] == snapshots[1] == snapshots[2]
    assert reports[0] == reports[1] == reports[2]
    constructor.assert_not_called()


@pytest.mark.parametrize("flags", [[], ["--infer-schema"]])
@pytest.mark.parametrize("renamed", ["all", "mandatory", "optional"])
def test_renamed_cli_reaches_existing_inference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    flags: list[str],
    renamed: str,
) -> None:
    labels = RENAMED.copy() if renamed == "all" else HEADERS.copy()
    if renamed != "all":
        labels[5 if renamed == "optional" else 0] = RENAMED[5 if renamed == "optional" else 0]
    source = write_workbook(
        tmp_path / "renamed.xlsx", headers=labels, header=5, rows=[asset(), asset()], summary=False
    )
    fake = FakeInference()
    constructor = Mock(return_value=fake)
    monkeypatch.setattr("sovtrace.ingestion.provider.OpenAISchemaInferenceProvider", constructor)
    db = tmp_path / "renamed.sqlite"
    main(["ingest", "--format", "json", str(source), "--db", str(db), *flags])
    report = json.loads(capsys.readouterr().out)
    constructor.assert_called_once_with()
    assert len(fake.requests) == 1
    assert [c.label for c in fake.requests[0].columns] == labels
    assert report["schema_inference"] == {
        "status": "applied",
        "model": None,
        "inferred_fields": list(EXPECTED)
        if renamed == "all"
        else ["construction_type" if renamed == "optional" else "asset_id"],
    }
    assert {f: m["column"] for f, m in saved_manifest(db)["concepts"].items()} == EXPECTED
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT total_insurable_value_micro_usd FROM assets").fetchall() == [
            (10000000000001,)
        ]
        assert c.execute("SELECT row_number FROM asset_sources ORDER BY row_number").fetchall() == [
            (6,),
            (7,),
        ]
        assert c.execute(
            "SELECT coordinate, original_value_json FROM source_cells WHERE column_index=5 ORDER BY row_number"
        ).fetchall() == [("E6", '"10000000.000001"'), ("E7", '"10000000.000001"')]


def test_cli_opt_out_rejects_renamed_without_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-unused-key")
    constructor = Mock(side_effect=AssertionError("Must not construct provider"))
    monkeypatch.setattr("sovtrace.ingestion.provider.OpenAISchemaInferenceProvider", constructor)
    source = write_workbook(tmp_path / "renamed.xlsx", headers=RENAMED, summary=False)
    db = tmp_path / "no.sqlite"
    with pytest.raises(SystemExit) as exc:
        main(["ingest", "--format", "json", str(source), "--db", str(db), "--no-infer-schema"])
    assert exc.value.code == 1
    assert "Reason: structure" in capsys.readouterr().err
    constructor.assert_not_called()
    assert not db.exists()


@pytest.mark.parametrize("mandatory_exact", [False, True])
def test_cli_missing_credentials_retains_failure_or_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mandatory_exact: bool,
) -> None:
    monkeypatch.setattr(
        "sovtrace.ingestion.provider.OpenAI",
        Mock(side_effect=AssertionError("No SDK client without credentials")),
    )
    labels = HEADERS.copy() if mandatory_exact else RENAMED.copy()
    labels[5] = RENAMED[5]
    source = write_workbook(tmp_path / "missing-key.xlsx", headers=labels, summary=False)
    db = tmp_path / "missing-key.sqlite"
    arguments = ["ingest", "--format", "json", str(source), "--db", str(db)]
    if not mandatory_exact:
        with pytest.raises(SystemExit) as exc:
            main(arguments)
        assert exc.value.code == 1
        assert (
            "Reason: schema-inference / schema_inference_provider_error: missing_credentials"
            in capsys.readouterr().err
        )
        assert not db.exists()
        return
    main(arguments)
    report = json.loads(capsys.readouterr().out)
    assert report["schema_inference"] == {
        "status": "fallback",
        "model": None,
        "inferred_fields": [],
    }
    assert report["diagnostic_summary"]["schema_inference_fallback"] == 1
    assert report["field_completeness"]["construction_type"]["availability"] == "absent"
    assert saved_manifest(db)["concepts"]["construction_type"]["status"] == "absent"
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT construction_type FROM assets").fetchone() == (None,)


@pytest.mark.parametrize("failure", ["invalid_proposal", "provider", "structure", "ambiguity"])
def test_cli_failed_default_replacement_preserves_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str,
) -> None:
    source = write_workbook(tmp_path / "known.xlsx")
    db = tmp_path / "existing.sqlite"
    main(["ingest", "--format", "json", str(source), "--db", str(db)])
    capsys.readouterr()
    before = db.read_bytes()
    labels = [*RENAMED, "TIV"] if failure == "ambiguity" else RENAMED
    source = write_workbook(
        tmp_path / "failed.xlsx",
        headers=labels,
        rows=[[*asset(), "other"]] if failure == "ambiguity" else None,
        summary=False,
        extra_table=failure == "structure",
    )
    fake = FakeInference(proposal({**EXPECTED, "city": 999}), fail=failure == "provider")
    constructor = Mock(return_value=fake)
    monkeypatch.setattr("sovtrace.ingestion.provider.OpenAISchemaInferenceProvider", constructor)
    with pytest.raises(SystemExit) as exc:
        main(["ingest", "--format", "json", str(source), "--db", str(db), "--replace"])
    assert exc.value.code == 1
    category = (
        "structure"
        if failure == "structure"
        else "schema-inference"
        if failure == "provider"
        else "mapping"
    )
    assert f"Reason: {category}" in capsys.readouterr().err
    assert db.read_bytes() == before
    assert not list(tmp_path.glob(".sov-import-*"))
    if failure in {"structure", "ambiguity"}:
        constructor.assert_not_called()
        assert not fake.requests
    else:
        assert len(fake.requests) == 1
