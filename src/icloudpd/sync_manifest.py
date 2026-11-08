"""SQLite-backed record of downloaded files, keyed by asset identity.

The manifest makes syncing with iCloud resumable and verifiable: a row that
matches the quick check (remote checksum unchanged, local file size unchanged)
proves that re-downloading the file would transfer the same bytes, so the
download is skipped without costing bandwidth.
"""

import hashlib
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from icloudpd.prune import lock_file


@dataclass(frozen=True)
class AssetRecord:
    """One local file representing (part of) one iCloud asset."""

    rel_path: str
    size: int
    sha256: str | None
    remote_checksum: str
    state: str


class SyncManifest:
    """Per-destination record of what has been downloaded, in SQLite.

    One row per local file, keyed by the iCloud asset record id. The same
    asset may produce several files (image and live photo video), and a
    converted file (HEIC replaced by JPEG) is recorded under its own path.
    Rows keep Apple's per-version checksum from the listing, so a remotely
    edited asset is detected without downloading it again.
    """

    filename = ".icloudpd-sync.db"

    def __init__(self, directory: str, scope: str, dry_run: bool = False) -> None:
        self.directory = Path(directory).resolve()
        self.path = self.directory / self.filename
        self.lock = self.directory / ".icloudpd-sync.lock"
        self.scope = scope
        self.dry_run = dry_run
        self.lock_handle: BinaryIO | None = None
        self.connection: sqlite3.Connection | None = None
        try:
            if self.lock.is_symlink():
                raise ValueError("Sync manifest lock must not be a symlink")
            if not dry_run:
                self.directory.mkdir(parents=True, exist_ok=True)
            if not dry_run or self.lock.exists():
                self.lock_handle = self.lock.open("r+b" if dry_run else "a+b")
                lock_file(self.lock_handle)
            if self.path.is_symlink():
                raise ValueError("Sync manifest must not be a symlink")
            if self.path.exists():
                self._verify_scope(self._connect(read_only=dry_run))
            elif not dry_run:
                self._initialize(self._connect(read_only=False))
        except Exception:
            self.close()
            raise

    def _connect(self, read_only: bool) -> sqlite3.Connection:
        if read_only:
            self.connection = sqlite3.connect(f"{self.path.as_uri()}?mode=ro", uri=True)
        else:
            self.connection = sqlite3.connect(self.path)
            self.connection.execute("PRAGMA synchronous=FULL")
            with self.connection:
                self.connection.execute(
                    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                self.connection.execute(
                    "CREATE TABLE IF NOT EXISTS assets ("
                    "asset_id TEXT NOT NULL, "
                    "rel_path TEXT NOT NULL, "
                    "size INTEGER NOT NULL, "
                    "sha256 TEXT, "
                    "remote_checksum TEXT NOT NULL, "
                    "state TEXT NOT NULL, "
                    "PRIMARY KEY (asset_id, rel_path))"
                )
        assert self.connection is not None
        return self.connection

    def _initialize(self, connection: sqlite3.Connection) -> None:
        with connection:
            connection.execute("INSERT INTO meta(key, value) VALUES('scope', ?)", (self.scope,))

    def _verify_scope(self, connection: sqlite3.Connection) -> None:
        row = connection.execute("SELECT value FROM meta WHERE key='scope'").fetchone()
        if row is None or row[0] != self.scope:
            raise ValueError("Sync manifest belongs to another account, library or configuration")

    def relative(self, path: str) -> str:
        """Path relative to the manifest directory, rejecting unsafe locations."""
        absolute = Path(path).parent.resolve() / Path(path).name
        try:
            relative = str(absolute.relative_to(self.directory))
        except ValueError as error:
            raise ValueError("Unsafe path for the sync manifest") from error
        parts = Path(relative).parts
        if (
            relative == "."
            or Path(relative).is_absolute()
            or ".." in parts
            or parts[0].startswith(".icloudpd-")
            or relative.endswith(".part")
        ):
            raise ValueError("Unsafe path for the sync manifest")
        return relative

    def find(self, asset_id: str, path: str) -> AssetRecord | None:
        """The row recorded for a specific local path, regardless of the quick check."""
        connection = self.connection
        if connection is None:
            return None
        row = connection.execute(
            "SELECT rel_path, size, sha256, remote_checksum, state FROM assets"
            " WHERE asset_id=? AND rel_path=?",
            (asset_id, self.relative(path)),
        ).fetchone()
        return None if row is None else AssetRecord(*row)

    def lookup(self, asset_id: str) -> list[AssetRecord]:
        """All rows recorded for an asset."""
        connection = self.connection
        if connection is None:
            return []
        return [
            AssetRecord(*row)
            for row in connection.execute(
                "SELECT rel_path, size, sha256, remote_checksum, state FROM assets"
                " WHERE asset_id=?",
                (asset_id,),
            )
        ]

    def _quick_check(self, record: AssetRecord, remote_checksum: str) -> bool:
        if record.remote_checksum != remote_checksum:
            return False
        path = self.directory / record.rel_path
        try:
            return path.is_file() and path.stat().st_size == record.size
        except OSError:
            return False

    def find_representation(
        self, asset_id: str, path: str, remote_checksum: str
    ) -> AssetRecord | None:
        """A record proving the asset is present locally without re-downloading.

        The exact row for the expected path, or a converted file for the same
        asset, in either case only when the quick check passes: the remote
        checksum is unchanged and the local file still has its recorded size.
        """
        record = self.find(asset_id, path)
        if record is not None and self._quick_check(record, remote_checksum):
            return record
        return next(
            (
                candidate
                for candidate in self.lookup(asset_id)
                if candidate.state == "converted" and self._quick_check(candidate, remote_checksum)
            ),
            None,
        )

    def adopt(self, asset_id: str, path: str, remote_checksum: str) -> None:
        """Record a pre-existing file; size only, content not verified."""
        connection = self.connection
        if connection is None or self.dry_run:
            return
        rel_path = self.relative(path)
        existing = connection.execute(
            "SELECT 1 FROM assets WHERE asset_id=? AND rel_path=?", (asset_id, rel_path)
        ).fetchone()
        if existing is not None:
            return
        with connection:
            connection.execute(
                "INSERT INTO assets"
                " (asset_id, rel_path, size, sha256, remote_checksum, state)"
                " VALUES(?, ?, ?, NULL, ?, 'adopted')",
                (asset_id, rel_path, os.path.getsize(path), remote_checksum),
            )

    def record(
        self, asset_id: str, path: str, remote_checksum: str, converted: bool = False
    ) -> None:
        """Record a downloaded file with its SHA-256."""
        connection = self.connection
        if connection is None or self.dry_run:
            return
        rel_path = self.relative(path)
        with open(path, "rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        with connection:
            connection.execute(
                "INSERT OR REPLACE INTO assets"
                " (asset_id, rel_path, size, sha256, remote_checksum, state)"
                " VALUES(?, ?, ?, ?, ?, ?)",
                (
                    asset_id,
                    rel_path,
                    os.path.getsize(path),
                    digest,
                    remote_checksum,
                    "converted" if converted else "downloaded",
                ),
            )

    def forget_absent(self, asset_ids: set[str]) -> int:
        """Drop rows for assets missing from a full listing; return the count."""
        connection = self.connection
        if connection is None or self.dry_run:
            return 0
        stored = {row[0] for row in connection.execute("SELECT DISTINCT asset_id FROM assets")}
        absent = stored - asset_ids
        if not absent:
            return 0
        with connection:
            connection.executemany(
                "DELETE FROM assets WHERE asset_id=?", [(asset_id,) for asset_id in absent]
            )
        return len(absent)

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        if self.lock_handle is not None:
            self.lock_handle.close()
            self.lock_handle = None
