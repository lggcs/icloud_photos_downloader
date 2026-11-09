"""Integration tests for the rsync-style sync manifest download flow."""

import datetime
import logging
import os
import shutil
import sqlite3
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import cast
from unittest import mock

import piexif
import pytest

from icloudpd.base import (
    DownloadResult,
    build_filename_cleaner,
    core_single_run,
    download_builder,
    lp_filename_concatinator,
)
from icloudpd.cli import parse
from icloudpd.convert import is_heic
from icloudpd.counter import Counter
from icloudpd.filename_policies import create_filename_builder
from icloudpd.status import StatusExchange
from icloudpd.string_helpers import truncate_middle
from icloudpd.sync_manifest import SyncManifest
from pyicloud_ipd.asset_version import AssetVersion
from pyicloud_ipd.base import PyiCloudService
from pyicloud_ipd.file_match import FileMatchPolicy
from pyicloud_ipd.item_type import AssetItemType
from pyicloud_ipd.raw_policy import RawTreatmentPolicy
from pyicloud_ipd.services.photos import PhotoAsset
from pyicloud_ipd.version_size import AssetVersionSize, LivePhotoVersionSize
from tests.helpers import (
    TestResult,
    path_from_project_root,
    run_cassette,
    run_main,
)
from tests.test_convert import make_heic
from tests.test_prune import library
from tests.test_prune_adoption import media_photo

LOGGER = logging.getLogger(__name__)
ROOT_PATH = path_from_project_root(__file__)
VCR_PATH = os.path.join(ROOT_PATH, "vcr_cassettes")
COOKIE_MASTER_PATH = os.path.join(ROOT_PATH, "cookie")


def manifest_rows(data_dir: Path) -> dict[str, tuple[int, str | None, str, str]]:
    """The sync manifest rows keyed by their os-native relative paths."""
    connection = sqlite3.connect(os.path.join(data_dir, SyncManifest.filename))
    try:
        rows = connection.execute(
            "SELECT rel_path, size, sha256, remote_checksum, state FROM assets"
        ).fetchall()
    finally:
        connection.close()
    return {
        rel_path: (size, sha256, remote_checksum, state)
        for rel_path, size, sha256, remote_checksum, state in rows
    }


def run_against_cassette(
    data_dir: Path, cookie_dir: Path, cassette: str, params: list[str]
) -> TestResult:
    """Run the CLI against a cassette with a fresh copy of the master cookies."""
    if cookie_dir.exists():
        shutil.rmtree(str(cookie_dir))
    shutil.copytree(COOKIE_MASTER_PATH, str(cookie_dir))
    return run_cassette(
        os.path.join(VCR_PATH, cassette),
        [
            "-d",
            str(data_dir),
            "--cookie-directory",
            str(cookie_dir),
            "--username",
            "jdoe@gmail.com",
            "--password",
            "password1",
            "--no-progress-bar",
            "--threads-num",
            "1",
            *params,
        ],
    )


def media_heic(identifier: str) -> mock.MagicMock:
    """A photo asset mock whose original version is a HEIC."""
    media = mock.MagicMock(spec=PhotoAsset)
    media.id = identifier
    media.created = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    media.filename = f"{identifier}.HEIC"
    media.calculate_filename.return_value = media.filename
    media.item_type_extension = "HEIC"
    media.item_type = AssetItemType.IMAGE
    media._asset_record = {"recordName": identifier, "fields": {}}
    media.versions_with_raw_policy.return_value = {
        AssetVersionSize.ORIGINAL: AssetVersion(
            100, "https://example.invalid/photo", "public.heic", "heic-checksum-1"
        ),
    }
    return media


def fake_download(
    _logger: logging.Logger,
    _dry_run: bool,
    _icloud: PyiCloudService,
    photo: PhotoAsset,
    download_path: str,
    _version: AssetVersion,
    _size: AssetVersionSize,
    _filename_builder: Callable[[PhotoAsset], str],
) -> bool:
    """Store real bytes where the download would put them."""
    Path(download_path).parent.mkdir(parents=True, exist_ok=True)
    if is_heic(download_path):
        make_heic(Path(download_path))
    else:
        Path(download_path).write_bytes(photo.id.encode())
    return True


def build_download(directory: Path, photo: PhotoAsset, manifest: SyncManifest) -> DownloadResult:
    """Run the real download pipeline for one photo, with conversion enabled."""
    return download_builder(
        logger=LOGGER,
        folder_structure="none",
        directory=str(directory),
        primary_sizes=[AssetVersionSize.ORIGINAL],
        force_size=False,
        only_print_filenames=False,
        set_exif_datetime=True,
        skip_live_photos=True,
        live_photo_size=LivePhotoVersionSize.ORIGINAL,
        dry_run=False,
        file_match_policy=FileMatchPolicy.NAME_SIZE_DEDUP_WITH_SUFFIX,
        xmp_sidecar=False,
        lp_filename_generator=lp_filename_concatinator,
        filename_builder=create_filename_builder(
            FileMatchPolicy.NAME_SIZE_DEDUP_WITH_SUFFIX, build_filename_cleaner(False)
        ),
        raw_policy=RawTreatmentPolicy.AS_IS,
        icloud=cast(PyiCloudService, mock.MagicMock()),
        counter=Counter(),
        photo=photo,
        sync_manifest=manifest,
        convert_to_jpeg=True,
    )


def sync_downloader(
    directory: Path, folder_structure: str = "none"
) -> Callable[..., DownloadResult]:
    """The production download pipeline, bound for core_single_run.

    The 15 configuration arguments must stay positional: core_single_run prepends
    the service and appends the counter and the photo when it invokes the downloader.
    """
    return partial(
        download_builder,
        LOGGER,
        folder_structure,
        str(directory),
        [AssetVersionSize.ORIGINAL],
        False,  # force_size
        False,  # only_print_filenames
        False,  # set_exif_datetime
        False,  # skip_live_photos
        LivePhotoVersionSize.ORIGINAL,
        False,  # dry_run
        FileMatchPolicy.NAME_SIZE_DEDUP_WITH_SUFFIX,
        False,  # xmp_sidecar
        lp_filename_concatinator,
        create_filename_builder(
            FileMatchPolicy.NAME_SIZE_DEDUP_WITH_SUFFIX, build_filename_cleaner(False)
        ),
        RawTreatmentPolicy.AS_IS,
    )


def run_sync(
    tmp_path: Path,
    current: mock.MagicMock,
    downloader: Callable[..., DownloadResult],
    extra: list[str],
) -> int:
    """Run core_single_run against a mocked shared library, wired like main() does."""
    global_config, configs = parse(
        [
            "--username",
            "user@example.com",
            "--directory",
            str(tmp_path),
            "--library",
            "SharedSync-test",
            "--no-progress-bar",
            *extra,
        ]
    )
    icloud = mock.MagicMock()
    icloud.photos.private_libraries = {}
    icloud.photos.shared_libraries = {"SharedSync-test": current}
    with mock.patch("icloudpd.base.authenticator", return_value=icloud):
        return core_single_run(
            LOGGER,
            StatusExchange(),
            global_config,
            configs[0],
            {},
            lambda _photo: True,
            downloader,
            lambda: None,
            lp_filename_concatinator,
        )


def test_sync_manifest_skips_downloads_on_second_run(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    for name, size in (("IMG_7407.JPG", 656257), ("IMG_7408.JPG", 1151066)):
        target = data_dir / "2018" / "07" / "30" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x" * size)

    params = ["--sync-manifest", "--recent", "5", "--skip-videos", "--skip-live-photos"]
    first = run_against_cassette(data_dir, tmp_path / "cookie", "listing_photos.yml", params)
    assert first.exit_code == 0
    img_7409 = os.path.join(str(data_dir), os.path.normpath("2018/07/31/IMG_7409.JPG"))
    assert f"Downloaded {truncate_middle(img_7409, 96)}" in first.output
    assert "All photos have been downloaded" in first.output

    rows = manifest_rows(data_dir)
    size, sha, _, state = rows[os.path.normpath("2018/07/31/IMG_7409.JPG")]
    assert state == "downloaded"
    assert sha is not None
    assert size == 1884695
    for name, expected_size in (("IMG_7407.JPG", 656257), ("IMG_7408.JPG", 1151066)):
        size, sha, _, state = rows[os.path.normpath(f"2018/07/30/{name}")]
        assert state == "adopted"
        assert sha is None
        assert size == expected_size

    second = run_against_cassette(
        data_dir, tmp_path / "cookie", "listing_photos.yml", [*params, "--log-level", "debug"]
    )
    assert second.exit_code == 0
    assert second.output.count("already in sync manifest") == 3
    assert f"Downloading {truncate_middle(img_7409, 96)}" not in second.output
    assert f"Downloaded {truncate_middle(img_7409, 96)}" not in second.output
    assert manifest_rows(data_dir).keys() == rows.keys()


def test_converts_heic_downloads_and_skips_refetching(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    params = ["--sync-manifest", "--convert-to-jpeg", "--recent", "3"]

    first = run_against_cassette(data_dir, tmp_path / "cookie", "download_live_photos.yml", params)
    assert first.exit_code == 0
    assert first.output.count("Downloaded ") == 5
    assert "All photos and videos have been downloaded" in first.output

    day = data_dir / "2020" / "11" / "04"
    for name in ("IMG_0516.HEIC", "IMG_0514.HEIC", "IMG_0512.HEIC"):
        assert not (day / name).exists()
    rows = manifest_rows(data_dir)
    assert set(rows) == {
        os.path.normpath(f"2020/11/04/{name}")
        for name in (
            "IMG_0516.JPG",
            "IMG_0514.JPG",
            "IMG_0512.JPG",
            "IMG_0514_HEVC.MOV",
            "IMG_0512_HEVC.MOV",
        )
    }
    for name in ("IMG_0516.JPG", "IMG_0514.JPG", "IMG_0512.JPG"):
        assert (day / name).is_file()
        _size, sha, _checksum, state = rows[os.path.normpath(f"2020/11/04/{name}")]
        assert state == "converted"
        assert sha is not None
    for name in ("IMG_0514_HEVC.MOV", "IMG_0512_HEVC.MOV"):
        assert (day / name).is_file()
        _size, sha, _checksum, state = rows[os.path.normpath(f"2020/11/04/{name}")]
        assert state == "downloaded"
        assert sha is not None

    second = run_against_cassette(
        data_dir, tmp_path / "cookie", "download_live_photos.yml", [*params, "--log-level", "debug"]
    )
    assert second.exit_code == 0
    assert second.output.count("already in sync manifest") == 5
    assert "Downloaded " not in second.output
    assert manifest_rows(data_dir).keys() == rows.keys()


def test_converted_jpeg_is_recorded_and_skipped_on_second_run(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    manifest = SyncManifest(str(tmp_path), '{"account": "sync-integration"}')
    download = mock.MagicMock(side_effect=fake_download)
    media = media_heic("IMG_4022")
    try:
        with mock.patch("icloudpd.base.download.download_media", new=download):
            first = build_download(tmp_path, cast(PhotoAsset, media), manifest)
            assert first.downloaded is True
            assert first.present is True
            assert download.call_count == 1

            assert not (tmp_path / "IMG_4022.HEIC").exists()
            jpeg = tmp_path / "IMG_4022.JPG"
            assert jpeg.is_file()
            assert piexif.load(str(jpeg))["Exif"].get(36867) is not None

            records = manifest.lookup("IMG_4022")
            assert len(records) == 1
            assert records[0].rel_path == "IMG_4022.JPG"
            assert records[0].state == "converted"
            assert records[0].sha256 is not None
            assert records[0].remote_checksum == "heic-checksum-1"

            with caplog.at_level(logging.DEBUG):
                second = build_download(tmp_path, cast(PhotoAsset, media), manifest)
            assert second.downloaded is False
            assert second.present is True
            assert download.call_count == 1
            assert "already in sync manifest" in caplog.text
    finally:
        manifest.close()


def test_verify_reports_up_to_date_missing_changed_and_outdated_files(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    photos = [media_photo(f"IMG_000{index}") for index in range(1, 4)]
    current = library(*photos)
    download = mock.MagicMock(side_effect=fake_download)
    download_params = ["--sync-manifest", "--folder-structure", "none"]
    verify_params = [*download_params, "--verify"]
    with mock.patch("icloudpd.base.download.download_media", new=download):
        assert run_sync(tmp_path, current, sync_downloader(tmp_path), download_params) == 0
        assert download.call_count == 3

        rows = manifest_rows(tmp_path)
        assert set(rows) == {"IMG_0001.JPG", "IMG_0002.JPG", "IMG_0003.JPG"}
        for _rel_path, (_size, sha, _checksum, state) in rows.items():
            assert state == "downloaded"
            assert sha is not None

        with caplog.at_level(logging.INFO):
            assert run_sync(tmp_path, current, sync_downloader(tmp_path), verify_params) == 0
        assert "Sync verification: 3 file(s) are up to date" in caplog.text

        (tmp_path / "IMG_0001.JPG").unlink()
        with caplog.at_level(logging.INFO):
            assert run_sync(tmp_path, current, sync_downloader(tmp_path), verify_params) == 1
        assert "IMG_0001.JPG is missing" in caplog.text

        with open(tmp_path / "IMG_0002.JPG", "ab") as target:
            target.write(b"x")
        with caplog.at_level(logging.INFO):
            assert run_sync(tmp_path, current, sync_downloader(tmp_path), verify_params) == 1
        assert "IMG_0002.JPG has changed since download" in caplog.text

        photos[2].versions_with_raw_policy.return_value = {
            AssetVersionSize.ORIGINAL: AssetVersion(
                8, "https://example.invalid/photo", "public.jpeg", "img-0003-checksum-2"
            ),
        }
        with caplog.at_level(logging.INFO):
            assert run_sync(tmp_path, current, sync_downloader(tmp_path), verify_params) == 1
        assert "IMG_0003.JPG is outdated: a newer version exists in iCloud" in caplog.text

        assert download.call_count == 3


def test_verify_checksums_detects_same_size_changes(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    current = library(media_photo("IMG_0001"))
    download = mock.MagicMock(side_effect=fake_download)
    download_params = ["--sync-manifest", "--folder-structure", "none"]
    with mock.patch("icloudpd.base.download.download_media", new=download):
        assert run_sync(tmp_path, current, sync_downloader(tmp_path), download_params) == 0
        assert download.call_count == 1

        target = tmp_path / "IMG_0001.JPG"
        target.write_bytes(b"y" * target.stat().st_size)

        with caplog.at_level(logging.INFO):
            assert (
                run_sync(
                    tmp_path, current, sync_downloader(tmp_path), [*download_params, "--verify"]
                )
                == 0
            )
        assert "Sync verification: 1 file(s) are up to date" in caplog.text

        caplog.clear()
        with caplog.at_level(logging.INFO):
            assert (
                run_sync(
                    tmp_path,
                    current,
                    sync_downloader(tmp_path),
                    [*download_params, "--verify", "--verify-checksums"],
                )
                == 1
            )
        assert "IMG_0001.JPG has changed since download" in caplog.text
        assert download.call_count == 1


def test_sync_manifest_scope_mismatch_is_rejected(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    current = library(media_photo("IMG_0001"))
    download = mock.MagicMock(side_effect=fake_download)
    with mock.patch("icloudpd.base.download.download_media", new=download):
        assert (
            run_sync(
                tmp_path,
                current,
                sync_downloader(tmp_path),
                ["--sync-manifest", "--folder-structure", "none"],
            )
            == 0
        )
        assert download.call_count == 1

        with caplog.at_level(logging.INFO):
            assert (
                run_sync(
                    tmp_path,
                    current,
                    sync_downloader(tmp_path, folder_structure="{:%Y}"),
                    ["--sync-manifest", "--folder-structure", "{:%Y}"],
                )
                == 1
            )
        assert "Cannot use the sync manifest" in caplog.text
        assert download.call_count == 1


def test_verify_follows_converted_jpegs(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    current = library(media_heic("IMG_4022"))
    download = mock.MagicMock(side_effect=fake_download)
    download_params = ["--sync-manifest", "--convert-to-jpeg", "--folder-structure", "none"]
    verify_params = [*download_params, "--verify"]
    with mock.patch("icloudpd.base.download.download_media", new=download):
        assert run_sync(tmp_path, current, sync_downloader(tmp_path), download_params) == 0
        assert download.call_count == 1
        assert not (tmp_path / "IMG_4022.HEIC").exists()
        assert (tmp_path / "IMG_4022.JPG").is_file()

        rows = manifest_rows(tmp_path)
        _size, sha, checksum, state = rows["IMG_4022.JPG"]
        assert state == "converted"
        assert sha is not None
        assert checksum == "heic-checksum-1"

        with caplog.at_level(logging.INFO):
            assert run_sync(tmp_path, current, sync_downloader(tmp_path), verify_params) == 0
        assert "Sync verification: 1 file(s) are up to date" in caplog.text
        assert download.call_count == 1

        (tmp_path / "IMG_4022.JPG").unlink()
        with caplog.at_level(logging.INFO):
            assert run_sync(tmp_path, current, sync_downloader(tmp_path), verify_params) == 1
        assert "IMG_4022.JPG is missing" in caplog.text


@pytest.mark.parametrize("extra", [["--convert-to-jpeg"], ["--verify"]])
def test_cli_requires_sync_manifest_for_convert_and_verify(extra: list[str]) -> None:
    result = run_main(["--username", "user@example.com", "--directory", "/unused", *extra])
    assert result.exit_code == 2
    assert "--convert-to-jpeg and --verify require --sync-manifest" in result.output


def test_cli_requires_verify_for_verify_checksums() -> None:
    result = run_main(
        [
            "--username",
            "user@example.com",
            "--directory",
            "/unused",
            "--sync-manifest",
            "--verify-checksums",
        ]
    )
    assert result.exit_code == 2
    assert "--verify-checksums requires --verify" in result.output


def test_cli_rejects_verify_with_partial_listing_options() -> None:
    result = run_main(
        [
            "--username",
            "user@example.com",
            "--directory",
            "/unused",
            "--sync-manifest",
            "--verify",
            "--recent",
            "1",
        ]
    )
    assert result.exit_code == 2
    assert "--verify requires a full library check and cannot be combined with" in result.output
