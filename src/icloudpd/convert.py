"""HEIC/HEIF to JPEG conversion for cross-platform mirrors."""

import contextlib
import logging
import os
import tempfile
from typing import Any, cast

CONVERSION_QUALITY = 92


def is_heic(path: str) -> bool:
    """Whether the path names a HEIC/HEIF file, by extension."""
    return os.path.splitext(path)[1].lower() in (".heic", ".heif")


def _drop_orientation(exif_bytes: bytes | None) -> bytes | None:
    """Remove the orientation tag: the pixels are already transposed."""
    if not exif_bytes:
        return exif_bytes
    import piexif

    try:
        exif_dict = piexif.load(exif_bytes)
    except Exception:
        return exif_bytes
    if exif_dict.get("0th", {}).get(piexif.ImageIFD.Orientation, 1) in (0, 1):
        return exif_bytes
    exif_dict["0th"].pop(piexif.ImageIFD.Orientation, None)
    try:
        return cast("bytes", piexif.dump(exif_dict))
    except Exception:
        return exif_bytes


def convert_heic_to_jpeg(logger: logging.Logger, source_path: str) -> str | None:
    """Convert a HEIC/HEIF file to JPEG next to it, removing the source.

    Returns the JPEG path on success, or None with the source kept on any
    failure. The JPEG carries over the EXIF metadata (with the orientation
    baked into the pixels and removed from the tags) and the color profile,
    and keeps the source's modification time.
    """
    try:
        from PIL import Image, ImageOps
        from pillow_heif import register_heif_opener
    except ImportError as error:
        logger.error("HEIC conversion requires the pillow-heif package: %s", error)
        return None

    destination_path = os.path.splitext(source_path)[0] + ".JPG"
    temporary_path = ""
    try:
        register_heif_opener()
        with Image.open(source_path) as image:
            exif_bytes = _drop_orientation(image.info.get("exif"))
            icc_profile = image.info.get("icc_profile")
            transposed = ImageOps.exif_transpose(image)
        if transposed.mode not in ("RGB", "L"):
            transposed = transposed.convert("RGB")
        with tempfile.NamedTemporaryFile(
            dir=os.path.dirname(source_path) or ".", delete=False, suffix=".jpg"
        ) as temporary:
            temporary_path = temporary.name
        save_kwargs: dict[str, Any] = {"quality": CONVERSION_QUALITY}
        if exif_bytes:
            save_kwargs["exif"] = exif_bytes
        if icc_profile:
            save_kwargs["icc_profile"] = icc_profile
        transposed.save(temporary_path, "JPEG", **save_kwargs)
        transposed.close()
        with Image.open(temporary_path) as check:
            check.verify()
        os.replace(temporary_path, destination_path)
        source_stat = os.stat(source_path)
        os.utime(destination_path, (source_stat.st_atime, source_stat.st_mtime))
        os.remove(source_path)
        return destination_path
    except Exception as error:
        with contextlib.suppress(OSError):
            if temporary_path and os.path.exists(temporary_path):
                os.remove(temporary_path)
        logger.error("Could not convert %s to JPEG: %s", source_path, error)
        logger.debug("HEIC conversion failed", exc_info=True)
        return None
