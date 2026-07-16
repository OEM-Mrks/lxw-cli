from __future__ import annotations

import json
from pathlib import Path

import pytest

from lxw_cli.core.errors import LexwareError
from lxw_cli.output import OutputFormat, print_count, render, working, write_binary


def test_print_count_capped(capsys) -> None:
    print_count(25, 100, noun="Artikel")
    err = capsys.readouterr().err
    assert "25 von 100 Artikel" in err
    assert "mehr mit --all" in err


def test_print_count_complete(capsys) -> None:
    print_count(100, 100, noun="Artikel")
    assert "100 von 100 Artikel" in capsys.readouterr().err


def test_print_count_unknown_total(capsys) -> None:
    print_count(7, None, noun="Treffer")
    assert "7 Treffer" in capsys.readouterr().err


def test_working_is_noop_without_terminal(capsys) -> None:
    # Under pytest stderr is not a TTY, so the spinner must stay silent and
    # leave both streams untouched.
    with working("Arbeitet …"):
        pass
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_working_propagates_exceptions() -> None:
    with pytest.raises(ValueError):
        with working("Arbeitet …"):
            raise ValueError("boom")


def test_render_json_writes_to_stdout(capsys) -> None:
    render([{"id": "a", "name": "Acme"}], OutputFormat.JSON)
    out = capsys.readouterr().out
    assert json.loads(out) == [{"id": "a", "name": "Acme"}]


def test_render_csv_to_file(tmp_path: Path) -> None:
    target = tmp_path / "out.csv"
    render(
        [{"id": "a", "name": "Acme"}, {"id": "b", "name": "Foo"}],
        OutputFormat.CSV,
        columns=["id", "name"],
        output_path=target,
    )
    content = target.read_text(encoding="utf-8")
    assert content.splitlines()[0] == "id,name"
    assert "a,Acme" in content
    assert "b,Foo" in content


def test_render_table_handles_paginated_envelope(capsys) -> None:
    render(
        {"content": [{"id": "a"}], "totalPages": 1, "last": True},
        OutputFormat.TABLE,
        columns=["id"],
    )
    out = capsys.readouterr().out
    assert "id" in out
    assert "a" in out


def test_write_binary_to_explicit_file(tmp_path: Path) -> None:
    target = tmp_path / "sub" / "rg.pdf"
    write_binary(b"%PDF-1.4", target, default_name="invoice-X.pdf")
    # Parent dir is created and the exact path is honored.
    assert target.read_bytes() == b"%PDF-1.4"


def test_write_binary_into_directory(tmp_path: Path) -> None:
    write_binary(b"data", tmp_path, default_name="invoice-RG1.pdf")
    assert (tmp_path / "invoice-RG1.pdf").read_bytes() == b"data"


def test_write_binary_none_uses_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    write_binary(b"data", None, default_name="invoice-RG3.pdf")
    assert (tmp_path / "invoice-RG3.pdf").read_bytes() == b"data"


def test_safe_filename_neutralizes_traversal() -> None:
    from lxw_cli.output import safe_filename

    assert safe_filename("invoice-../../etc/x.pdf") == "invoice-.._.._etc_x.pdf"
    assert safe_filename("..\\evil.pdf") == "_evil.pdf"
    assert safe_filename("...") == "datei"
    assert safe_filename("invoice-FB2600682.pdf") == "invoice-FB2600682.pdf"


def test_write_binary_default_name_cannot_escape_directory(tmp_path: Path) -> None:
    write_binary(b"data", tmp_path, default_name="invoice-../escape.pdf")
    # The sanitized file lands inside tmp_path; nothing is written above it.
    assert (tmp_path / "invoice-.._escape.pdf").read_bytes() == b"data"
    assert not (tmp_path.parent / "escape.pdf").exists()


def test_write_binary_unwritable_raises_clean_error(tmp_path: Path) -> None:
    # A path under an existing *file* can't be created → clean LexwareError,
    # not a raw OSError traceback.
    a_file = tmp_path / "afile"
    a_file.write_text("x")
    with pytest.raises(LexwareError, match="Konnte Datei nicht schreiben"):
        write_binary(b"data", a_file / "nested.pdf", default_name="x.pdf")
