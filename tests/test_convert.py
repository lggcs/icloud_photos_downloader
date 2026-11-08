import logging
import os
from pathlib import Path
from typing import Any

import piexif
import pytest
from PIL import Image
from pillow_heif import register_heif_opener

from icloudpd.convert import _drop_orientation, convert_heic_to_jpeg, is_heic

LOGGER = logging.getLogger("icloudpd-convert-test")
register_heif_opener()


def make_heic(
    path: Path,
    width: int = 40,
    height: int = 30,
    orientation: int | None = None,
    with_exif_date: bool = False,
    with_icc: bool = False,
) -> str:
    image = Image.new("RGB", (width, height), (200, 60, 60))
    exif_dict: dict[str, Any] = {"0th": {}, "Exif": {}}
    if orientation is not None:
        exif_dict["0th"][piexif.ImageIFD.Orientation] = orientation
    if with_exif_date:
        exif_dict["Exif"][piexif.ExifIFD.DateTimeOriginal] = b"2024:07:30 07:22:24"
    kwargs: dict[str, Any] = {"exif": piexif.dump(exif_dict)}
    if with_icc:
        kwargs["icc_profile"] = b"fake-icc-profile"
    image.save(str(path), "HEIF", **kwargs)
    return str(path)


def test_is_heic() -> None:
    assert is_heic("IMG_1234.HEIC")
    assert is_heic("IMG_1234.heic")
    assert is_heic("photos/IMG_1234.HEIF")
    assert not is_heic("IMG_1234.JPG")
    assert not is_heic("IMG_1234.mov")
    assert not is_heic("IMG_1234")


def test_converts_and_removes_source(tmp_path: Path) -> None:
    source = make_heic(tmp_path / "IMG_4022.HEIC")
    result = convert_heic_to_jpeg(LOGGER, source)
    assert result == str(tmp_path / "IMG_4022.JPG")
    assert not os.path.exists(source)
    with Image.open(result) as jpeg:
        assert jpeg.format == "JPEG"
        assert jpeg.size == (40, 30)


def test_bakes_orientation_into_pixels(tmp_path: Path) -> None:
    source = make_heic(tmp_path / "IMG_4023.HEIC", width=20, height=10, orientation=6)
    result = convert_heic_to_jpeg(LOGGER, source)
    assert result is not None
    with Image.open(result) as jpeg:
        assert jpeg.size == (10, 20)
        exif_dict = piexif.load(jpeg.info.get("exif", b""))
        # pillow-heif applies HEIF rotation on load, so the pixels arrive
        # upright and an upright tag (1) may remain behind; only a residual
        # rotation tag (2-8) would wrongly rotate the JPEG a second time
        assert exif_dict["0th"].get(piexif.ImageIFD.Orientation, 1) in (0, 1)


def test_converts_heic_without_exif(tmp_path: Path) -> None:
    source = tmp_path / "IMG_4027.HEIC"
    Image.new("RGB", (40, 30), (60, 120, 200)).save(str(source), "HEIF")
    result = convert_heic_to_jpeg(LOGGER, str(source))
    assert result == str(tmp_path / "IMG_4027.JPG")
    with Image.open(result) as jpeg:
        assert jpeg.size == (40, 30)
    assert not os.path.exists(str(source))


def test_drop_orientation_removes_only_transposed_tag() -> None:
    rotated = piexif.dump({"0th": {piexif.ImageIFD.Orientation: 6}, "Exif": {}})
    dropped = _drop_orientation(rotated)
    assert dropped is not None
    assert piexif.ImageIFD.Orientation not in piexif.load(dropped)["0th"]
    upright = piexif.dump({"0th": {piexif.ImageIFD.Orientation: 1}, "Exif": {}})
    assert _drop_orientation(upright) == upright
    assert _drop_orientation(None) is None


def test_preserves_exif_date_and_color_profile(tmp_path: Path) -> None:
    source = make_heic(tmp_path / "IMG_4024.HEIC", with_exif_date=True, with_icc=True)
    result = convert_heic_to_jpeg(LOGGER, source)
    assert result is not None
    with Image.open(result) as jpeg:
        exif_dict = piexif.load(jpeg.info.get("exif", b""))
        assert exif_dict["Exif"][piexif.ExifIFD.DateTimeOriginal] == b"2024:07:30 07:22:24"
        assert jpeg.info.get("icc_profile") == b"fake-icc-profile"


def test_preserves_xmp_and_gps_metadata(tmp_path: Path) -> None:
    xmp_packet = (
        b'<x:xmpmeta xmlns:x="adobe:ns:meta/">'
        b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        b"<rdf:Description/></rdf:RDF></x:xmpmeta>"
    )
    exif_dict: dict[str, Any] = {
        "0th": {},
        "Exif": {},
        "GPS": {
            piexif.GPSIFD.GPSLatitudeRef: b"N",
            piexif.GPSIFD.GPSLatitude: ((40, 1), (26, 1), (45, 1)),
        },
    }
    source = tmp_path / "IMG_4028.HEIC"
    Image.new("RGB", (40, 30), (200, 60, 60)).save(
        str(source), "HEIF", exif=piexif.dump(exif_dict), xmp=xmp_packet
    )
    result = convert_heic_to_jpeg(LOGGER, str(source))
    assert result is not None
    with Image.open(result) as jpeg:
        assert jpeg.info.get("xmp") == xmp_packet
        gps = piexif.load(jpeg.info.get("exif", b""))["GPS"]
        assert gps[piexif.GPSIFD.GPSLatitudeRef] == b"N"
        assert gps[piexif.GPSIFD.GPSLatitude] == ((40, 1), (26, 1), (45, 1))


def test_preserves_source_mtime(tmp_path: Path) -> None:
    source = make_heic(tmp_path / "IMG_4025.HEIC")
    stamp = 1_700_000_000.0
    os.utime(source, (stamp, stamp))
    result = convert_heic_to_jpeg(LOGGER, source)
    assert result is not None
    assert os.path.getmtime(result) == pytest.approx(stamp)


def test_failure_keeps_source(tmp_path: Path) -> None:
    source = tmp_path / "IMG_4026.HEIC"
    source.write_bytes(b"this is not a HEIC file")
    assert convert_heic_to_jpeg(LOGGER, str(source)) is None
    assert source.read_bytes() == b"this is not a HEIC file"
    assert not (tmp_path / "IMG_4026.JPG").exists()
    assert list(tmp_path.glob("*.jpg")) == []
