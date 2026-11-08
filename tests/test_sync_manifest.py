import logging
from pathlib import Path

import pytest

from icloudpd.prune import lock_file
from icloudpd.sync_manifest import SyncManifest

LOGGER = logging.getLogger("icloudpd-sync-test")
SCOPE = '{"account": "jdoe@gmail.com", "settings": {"sizes": "original"}}'


@pytest.fixture
def manifest(tmp_path: Path) -> SyncManifest:
    result = SyncManifest(str(tmp_path), SCOPE)
    try:
        yield result
    finally:
        result.close()


def write_file(directory: Path, name: str, content: bytes = b"photo-data") -> str:
    target = directory / name
    target.write_bytes(content)
    return str(target)


def test_fresh_manifest_creates_database(tmp_path: Path) -> None:
    manifest = SyncManifest(str(tmp_path), SCOPE)
    try:
        assert (tmp_path / SyncManifest.filename).is_file()
        assert manifest.find("asset-1", str(tmp_path / "IMG_1.JPG")) is None
        assert manifest.lookup("asset-1") == []
    finally:
        manifest.close()


def test_scope_mismatch_is_refused_and_releases_lock(tmp_path: Path) -> None:
    manifest = SyncManifest(str(tmp_path), SCOPE)
    manifest.close()
    with pytest.raises(ValueError):
        SyncManifest(str(tmp_path), '{"account": "other@example.com"}')
    # the failed instance must not leave the lock behind
    with (tmp_path / ".icloudpd-sync.lock").open("a+b") as handle:
        lock_file(handle)


def test_record_passes_quick_check_until_remote_or_local_change(
    tmp_path: Path, manifest: SyncManifest
) -> None:
    path = write_file(tmp_path, "IMG_1.JPG")
    manifest.record("asset-1", path, "checksum-1")
    record = manifest.find("asset-1", path)
    assert record is not None
    assert record.size == len(b"photo-data")
    assert record.sha256 is not None
    assert record.state == "downloaded"
    assert manifest.find_representation("asset-1", path, "checksum-1") == record
    # the asset was edited in iCloud: it must download again
    assert manifest.find_representation("asset-1", path, "checksum-2") is None
    # the local file was modified: it must download again
    # (a modification that keeps the byte size is only caught by --verify-checksums)
    Path(path).write_bytes(b"locally-modified-content")
    assert manifest.find_representation("asset-1", path, "checksum-1") is None
    # the local file was deleted: the row stays for verification
    Path(path).unlink()
    assert manifest.find("asset-1", path) is not None


def test_converted_file_satisfies_heic_expectation(tmp_path: Path, manifest: SyncManifest) -> None:
    heic = write_file(tmp_path, "IMG_2.HEIC")
    jpeg = write_file(tmp_path, "IMG_2.JPG", b"converted-jpeg")
    manifest.record("asset-2", jpeg, "heic-checksum", converted=True)
    # the HEIC is expected on the next run, but the JPEG represents the asset
    assert manifest.find_representation("asset-2", heic, "heic-checksum") is not None
    # a different size rendition has its own checksum and still downloads
    assert manifest.find_representation("asset-2", heic, "medium-checksum") is None
    # a replaced or truncated JPEG no longer represents the asset
    Path(jpeg).write_bytes(b"")
    assert manifest.find_representation("asset-2", heic, "heic-checksum") is None


def test_adopt_records_size_only_without_overwriting(
    tmp_path: Path, manifest: SyncManifest
) -> None:
    path = write_file(tmp_path, "IMG_3.JPG")
    manifest.adopt("asset-3", path, "checksum-3")
    record = manifest.find("asset-3", path)
    assert record is not None
    assert record.sha256 is None
    assert record.state == "adopted"
    # repeated adoption does not churn the row
    manifest.adopt("asset-3", path, "checksum-3")
    assert manifest.lookup("asset-3") == [record]
    # a changed remote checksum must not be adopted over: the row keeps the truth
    manifest.adopt("asset-3", path, "checksum-changed")
    assert manifest.find("asset-3", path) == record


def test_forget_absent_removes_unlisted_assets(tmp_path: Path, manifest: SyncManifest) -> None:
    kept = write_file(tmp_path, "IMG_4.JPG")
    gone = write_file(tmp_path, "IMG_5.JPG")
    manifest.record("kept-asset", kept, "checksum-4")
    manifest.record("gone-asset", gone, "checksum-5")
    assert manifest.forget_absent({"kept-asset", "extra-asset"}) == 1
    assert manifest.lookup("kept-asset") != []
    assert manifest.lookup("gone-asset") == []


def test_dry_run_reads_but_never_writes(tmp_path: Path, manifest: SyncManifest) -> None:
    path = write_file(tmp_path, "IMG_6.JPG")
    manifest.record("asset-6", path, "checksum-6")
    manifest.close()
    dry = SyncManifest(str(tmp_path), SCOPE, dry_run=True)
    try:
        other = write_file(tmp_path, "IMG_7.JPG")
        assert dry.find("asset-6", path) is not None
        dry.adopt("asset-7", other, "checksum-7")
        dry.record("asset-7", other, "checksum-7")
        assert dry.forget_absent(set()) == 0
        assert dry.lookup("asset-7") == []
    finally:
        dry.close()
    reopened = SyncManifest(str(tmp_path), SCOPE)
    try:
        assert reopened.lookup("asset-7") == []
        assert reopened.lookup("asset-6") != []
    finally:
        reopened.close()


def test_lock_excludes_second_instance(tmp_path: Path, manifest: SyncManifest) -> None:
    with pytest.raises(OSError):
        SyncManifest(str(tmp_path), SCOPE)
    # after close the lock is free again
    manifest.close()
    second = SyncManifest(str(tmp_path), SCOPE)
    second.close()


def test_unsafe_paths_are_rejected(tmp_path: Path, manifest: SyncManifest) -> None:
    outside = tmp_path.parent / "outside.JPG"
    with pytest.raises(ValueError):
        manifest.record("asset-8", str(outside), "checksum-8")
    with pytest.raises(ValueError):
        manifest.record("asset-8", str(tmp_path / ".icloudpd-sync.db"), "checksum-8")
