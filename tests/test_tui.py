"""Tests for the interactive backup helper."""

import sys
from pathlib import Path
from unittest import mock

import pytest

from icloudpd.tui import (
    BASE_FLAGS,
    MENU,
    ask,
    build_command,
    choose_action,
    icloudpd_argv,
    main,
    prompt_required,
)


def test_actions_share_base_flags() -> None:
    """Backup and verify must not drift apart, or the manifest is refused."""
    for _key, _label, flags in MENU:
        if flags is None:
            continue
        assert flags[: len(BASE_FLAGS)] == BASE_FLAGS


def test_build_command() -> None:
    assert build_command("/data", "user@example.com", BASE_FLAGS) == [
        "--directory",
        "/data",
        "--username",
        "user@example.com",
        "--sync-manifest",
        "--convert-to-jpeg",
    ]


def test_commands_carry_no_password() -> None:
    """The password must be handled by icloudpd itself, never by the command line."""
    for _key, _label, flags in MENU:
        if flags is None:
            continue
        argv = icloudpd_argv(build_command("/data", "user@example.com", flags))
        assert "--password" not in argv


def test_icloudpd_argv_uses_the_running_interpreter() -> None:
    assert icloudpd_argv(["--version"]) == [sys.executable, "-m", "icloudpd", "--version"]


def test_ask_returns_default_on_empty_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("builtins.input", mock.Mock(return_value="  "))
    assert ask("Backup directory", "~/iCloud") == "~/iCloud"


def test_prompt_required_reprompts_on_empty_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("builtins.input", mock.Mock(side_effect=["", "user@example.com"]))
    assert prompt_required("iCloud account (email)") == "user@example.com"


@pytest.mark.parametrize(
    ("choice", "expected"),
    [
        ("1", BASE_FLAGS),
        ("2", [*BASE_FLAGS, "--verify"]),
        ("3", [*BASE_FLAGS, "--verify", "--verify-checksums"]),
        ("q", None),
        ("Q", None),
    ],
)
def test_choose_action_returns_flags_for_choice(
    monkeypatch: pytest.MonkeyPatch, choice: str, expected: list[str] | None
) -> None:
    monkeypatch.setattr("builtins.input", mock.Mock(return_value=choice))
    assert choose_action() == expected


def test_choose_action_reprompts_on_invalid_choice(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("builtins.input", mock.Mock(side_effect=["x", "1"]))
    assert choose_action() == BASE_FLAGS
    assert "Please answer 1, 2, 3 or q." in capsys.readouterr().out


def test_main_runs_backup_then_quits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    backup_dir = tmp_path / "backup"
    monkeypatch.setattr(
        "builtins.input",
        mock.Mock(side_effect=["user@example.com", str(backup_dir), "1", "q"]),
    )
    run = mock.Mock(return_value=mock.Mock(returncode=0))
    monkeypatch.setattr("icloudpd.tui.subprocess.run", run)

    assert main() == 0

    run.assert_called_once_with(
        [
            sys.executable,
            "-m",
            "icloudpd",
            "--directory",
            str(backup_dir),
            "--username",
            "user@example.com",
            *BASE_FLAGS,
        ],
        check=False,
    )
    output = capsys.readouterr().out
    assert "Running:" in output
    assert "icloudpd finished successfully." in output
    assert backup_dir.is_dir()


def test_main_verify_uses_the_same_scope_flags_as_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup_dir = tmp_path / "backup"
    monkeypatch.setattr(
        "builtins.input",
        mock.Mock(side_effect=["user@example.com", str(backup_dir), "2", "q"]),
    )
    run = mock.Mock(return_value=mock.Mock(returncode=0))
    monkeypatch.setattr("icloudpd.tui.subprocess.run", run)

    assert main() == 0

    run.assert_called_once_with(
        [
            sys.executable,
            "-m",
            "icloudpd",
            "--directory",
            str(backup_dir),
            "--username",
            "user@example.com",
            *BASE_FLAGS,
            "--verify",
        ],
        check=False,
    )


def test_main_quits_without_running_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "builtins.input",
        mock.Mock(side_effect=["user@example.com", str(tmp_path / "backup"), "q"]),
    )
    run = mock.Mock()
    monkeypatch.setattr("icloudpd.tui.subprocess.run", run)

    assert main() == 0
    run.assert_not_called()


def test_main_reports_a_failing_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    backup_dir = tmp_path / "backup"
    monkeypatch.setattr(
        "builtins.input",
        mock.Mock(side_effect=["user@example.com", str(backup_dir), "1", "q"]),
    )
    monkeypatch.setattr(
        "icloudpd.tui.subprocess.run",
        mock.Mock(return_value=mock.Mock(returncode=1)),
    )

    assert main() == 0
    assert "icloudpd exited with code 1" in capsys.readouterr().out


def test_main_returns_to_menu_after_an_interrupted_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    backup_dir = tmp_path / "backup"
    monkeypatch.setattr(
        "builtins.input",
        mock.Mock(side_effect=["user@example.com", str(backup_dir), "1", "q"]),
    )
    run = mock.Mock(side_effect=KeyboardInterrupt)
    monkeypatch.setattr("icloudpd.tui.subprocess.run", run)

    assert main() == 0
    run.assert_called_once()
    assert "Interrupted." in capsys.readouterr().out


def test_main_ends_the_session_when_input_closes_at_the_menu(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "builtins.input",
        mock.Mock(side_effect=["user@example.com", str(tmp_path / "backup"), EOFError]),
    )
    run = mock.Mock()
    monkeypatch.setattr("icloudpd.tui.subprocess.run", run)

    assert main() == 0
    run.assert_not_called()


def test_main_ends_the_session_when_input_closes_at_the_first_prompt(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("builtins.input", mock.Mock(side_effect=[EOFError]))
    run = mock.Mock()
    monkeypatch.setattr("icloudpd.tui.subprocess.run", run)

    assert main() == 0
    assert "Session ended." in capsys.readouterr().out


def test_main_reprompts_on_empty_username(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "builtins.input",
        mock.Mock(side_effect=["", "user@example.com", str(tmp_path / "backup"), "q"]),
    )
    run = mock.Mock()
    monkeypatch.setattr("icloudpd.tui.subprocess.run", run)

    assert main() == 0
    assert "Please enter a value." in capsys.readouterr().out
    run.assert_not_called()


def test_main_fails_cleanly_when_the_directory_cannot_be_created(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    blocker = tmp_path / "file.txt"
    blocker.write_text("not a directory")
    monkeypatch.setattr(
        "builtins.input",
        mock.Mock(side_effect=["user@example.com", str(blocker / "sub")]),
    )
    run = mock.Mock()
    monkeypatch.setattr("icloudpd.tui.subprocess.run", run)

    assert main() == 1
    assert "Cannot create the backup directory" in capsys.readouterr().out
    run.assert_not_called()
