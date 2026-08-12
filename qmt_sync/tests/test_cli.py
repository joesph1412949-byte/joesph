from qmt_sync import cli


def test_version(capsys):
    assert cli.main(["--version"]) == 0
    assert "qmt_sync" in capsys.readouterr().out


def test_import_statement_placeholder(capsys):
    assert cli.main(["--import-statement", "x.xlsx"]) == 2
    assert "Phase 2" in capsys.readouterr().err


def test_unknown_flag():
    import pytest
    with pytest.raises(SystemExit):
        cli.main(["--nope"])
