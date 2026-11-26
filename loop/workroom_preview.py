"""Read-only, bounded representations for the workroom file viewer."""
# 主要引用方包括 `workbench.py`。

import base64
from io import BytesIO
import re
import warnings

BYTE_PAGE = 1024
MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000


def raster_format(header):
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG"
    if header.startswith(b"\xff\xd8\xff"):
        return "JPEG"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "GIF"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "WEBP"
    if header.startswith(b"BM") and header[6:10] == bytes(4):
        return "BMP"
    if header.startswith(b"\x00\x00\x01\x00"):
        return "ICO"
    if header.startswith((b"II*\x00", b"MM\x00*")):
        return "TIFF"
    return None


def binary_block(data, *, first):
    # Leave line breaks, form feeds, backspaces and ANSI escape sequences in text logs intact.
    return bool(re.search(rb"[\x00-\x07\x0e-\x1a\x1c-\x1f]", data)
                or first and (raster_format(data) or re.match(rb"%PDF-[0-9]\.[0-9]", data)))


def bytes_page(raw, offset, size, reason=""):
    chunk = raw[:BYTE_PAGE]
    end = offset + len(chunk)
    return dict(kind="bytes", hex=chunk.hex(), offset=offset, nextOffset=end, hasMore=end < size,
                reason=reason)


def image_page(stream, header, size, fmt):
    def fallback(reason):
        return bytes_page(header, 0, size, reason)

    if size > MAX_IMAGE_BYTES:
        return fallback("image_limit")
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return fallback("decoder_unavailable")
    stream.seek(0)
    raw = stream.read(size)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(raw), formats=[fmt]) as image:
                width, height = image.size
                if width * height > MAX_IMAGE_PIXELS:
                    return fallback("image_limit")
                if image.getexif().get(274) in (5, 6, 7, 8):
                    width, height = height, width
                first_frame = bool(getattr(image, "is_animated", False))
                # Fit even an incompressible RGBA preview into the 6 MiB Electron response.
                image.thumbnail((1024, 1024))
                preview = ImageOps.exif_transpose(image).convert("RGBA")
                preview.info.clear()
                output = BytesIO()
                preview.save(output, format="PNG")
        return dict(kind="image", format=fmt, width=width, height=height, firstFrame=first_frame,
                    png=base64.b64encode(output.getvalue()).decode("ascii"))
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        return fallback("image_limit")
    except Exception:
        # A damaged image is still a readable file. Only the in-memory decoder may fall back.
        return fallback("invalid_image")
