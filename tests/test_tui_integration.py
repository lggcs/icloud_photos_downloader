"""Integration tests for the interactive helper, without iCloud credentials.

The real icloudpd needs a working login, which tests do not have. Instead of
stubbing subprocess (done in tests/test_tui.py), these tests let the helper
spawn a real child process: a tiny icloudpd stand-in package shadows the
installed one via the current directory, so process spawn, inherited stdout,
exact argument delivery and exit-code propagation are all exercised — with no
network and no login.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

TUI_SOURCE = Path(__file__).resolve().parent.parent / "src" / "icloudpd" / "tui.py"

STUB_MAIN = """\
import os
import sys

print("STUB-ICLOUDPD", " ".join(sys.argv[1:]))
sys.exit(int(os.environ.get("STUB_ICLOUDPD_EXIT", "0")))
"""


def stub_session(tmp_path: Path, stub_exit: str) -> subprocess.CompletedProcess[str]:
    """Run one full TUI session whose backup action spawns the stub icloudpd."""
    package = tmp_path / "icloudpd"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "__main__.py").write_text(STUB_MAIN)
    shutil.copyfile(TUI_SOURCE, package / "tui.py")

    target = tmp_path / "backup"
    # One answer per prompt: account, directory, backup action, quit.
    return subprocess.run(
        [sys.executable, "-m", "icloudpd.tui"],
        cwd=tmp_path,
        input=f"user@example.com\n{target}\n1\nq\n",
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "STUB_ICLOUDPD_EXIT": stub_exit},
        check=False,
    )


def test_session_spawns_backup_and_reports_success(tmp_path: Path) -> None:
    target = tmp_path / "backup"
    session = stub_session(tmp_path, "0")

    assert session.returncode == 0
    assert target.is_dir()
    # The printed command is the one a user can rerun by hand.
    assert (
        f"Running: {sys.executable} -m icloudpd --directory {target} "
        "--username user@example.com --sync-manifest --convert-to-jpeg"
    ) in session.stdout
    # The child received exactly the arguments the helper printed.
    assert (
        f"STUB-ICLOUDPD --directory {target} "
        "--username user@example.com --sync-manifest --convert-to-jpeg"
    ) in session.stdout
    assert "icloudpd finished successfully." in session.stdout


def test_session_reports_a_failing_backup_and_returns_to_menu(tmp_path: Path) -> None:
    session = stub_session(tmp_path, "3")

    assert session.returncode == 0
    assert "STUB-ICLOUDPD" in session.stdout
    assert "icloudpd exited with code 3." in session.stdout
    assert "icloudpd finished successfully." not in session.stdout
    # The menu came back and closed the session cleanly.
    assert session.stdout.count("What would you like to do?") == 2
