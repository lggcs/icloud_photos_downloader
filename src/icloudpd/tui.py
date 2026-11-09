"""Interactive helper for the resumable, rsync-style iCloud backup.

Runs the real icloudpd command as a child process, so the login prompt,
two-factor codes and the progress bar behave exactly like a manual run.
The exact command is printed before it runs, so every action can be
reproduced by hand.
"""

import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

DEFAULT_DIRECTORY = "~/iCloud"

# Every action shares the same base flags: the sync manifest's scope guard
# refuses a manifest created with different settings, so backup and verify
# must not drift apart.
BASE_FLAGS = ["--sync-manifest", "--convert-to-jpeg"]

MENU: list[tuple[str, str, list[str] | None]] = [
    ("1", "Backup — download new and changed items (resumable, HEIC to JPEG)", BASE_FLAGS),
    ("2", "Verify — check the backup against iCloud, downloads nothing", [*BASE_FLAGS, "--verify"]),
    (
        "3",
        "Verify deep — also compare SHA-256 checksums of every local file",
        [*BASE_FLAGS, "--verify", "--verify-checksums"],
    ),
    ("q", "Quit", None),
]


class SessionEnded(Exception):
    """Raised when the interactive session ends because the input closed."""


def ask(message: str, default: str | None = None) -> str:
    """Read one answer, returning the default on empty input."""
    suffix = f" [{default}]" if default is not None else ""
    try:
        answer = input(f"{message}{suffix}: ").strip()
    except EOFError as input_closed:
        raise SessionEnded from input_closed
    return answer or (default or "")


def prompt_required(message: str) -> str:
    """Read an answer, re-prompting until it is not empty."""
    while True:
        answer = ask(message)
        if answer:
            return answer
        print("Please enter a value.")


def choose_action() -> list[str] | None:
    """Show the menu and return the chosen action's flags, or None to quit."""
    print()
    print("What would you like to do?")
    for key, label, _flags in MENU:
        print(f"  {key}  {label}")
    while True:
        try:
            choice = input("Choice: ").strip().lower()
        except EOFError:
            return None
        for key, _label, flags in MENU:
            if choice == key:
                return flags
        print("Please answer 1, 2, 3 or q.")


def build_command(directory: str, username: str, flags: Sequence[str]) -> list[str]:
    """The icloudpd arguments for one action, without the interpreter prefix."""
    return ["--directory", directory, "--username", username, *flags]


def icloudpd_argv(arguments: Sequence[str]) -> list[str]:
    """The full command line, using the same interpreter that runs the TUI."""
    return [sys.executable, "-m", "icloudpd", *arguments]


def report(returncode: int) -> None:
    """Summarize the exit code of one icloudpd run."""
    if returncode == 0:
        print("icloudpd finished successfully.")
    else:
        print(f"icloudpd exited with code {returncode}. See the messages above for details.")


def run_action(directory: str, username: str, flags: Sequence[str]) -> int:
    """Run one icloudpd action as a child process and return its exit code."""
    argv = icloudpd_argv(build_command(directory, username, flags))
    print()
    print(f"Running: {' '.join(argv)}")
    try:
        completed = subprocess.run(argv, check=False)
    except KeyboardInterrupt:
        print()
        print("Interrupted.")
        return 130
    return completed.returncode


def main() -> int:
    """Prompt for the account and run backup or verify actions from a menu."""
    print("iCloud Backup — resumable icloudpd helper")
    print()
    print(
        "Your password is requested by icloudpd itself (hidden input, stored in "
        "your system keyring after the first successful login) and never appears "
        "on the command line."
    )
    try:
        username = prompt_required("iCloud account (email)")
        directory = ask("Backup directory", DEFAULT_DIRECTORY)
        target = Path(directory).expanduser()
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            print(f"Cannot create the backup directory {directory}: {error}")
            return 1
        while True:
            flags = choose_action()
            if flags is None:
                return 0
            report(run_action(str(target), username, flags))
    except SessionEnded:
        print()
        print("Session ended.")
        return 0
    except KeyboardInterrupt:
        print()
        print("Interrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
