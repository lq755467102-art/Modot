"""Deterministic image transforms in a memory/time bounded child process."""

# 主要引用或加载入口包括 `parser_process.py`。

import base64
from io import BytesIO
import json
from pathlib import Path
import sys
import warnings


# 由 `parser_process.py` 交入图片副本、哈希和允许的转换参数。
# 输出附实际尺寸和图片类型供宿主登记为成果。
def transform(request):
    from PIL import Image, ImageOps

    Image.MAX_IMAGE_PIXELS = 20_000_000
    warnings.simplefilter("error", Image.DecompressionBombWarning)
    raw = Path(request["path"]).read_bytes()
    import hashlib

    if len(raw) > 2 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != request["hash"]:
        raise ValueError("输入图片版本已变化")
    with Image.open(BytesIO(raw)) as original:
        if original.format not in {"PNG", "JPEG"}:
            raise ValueError("当前仅支持 PNG/JPEG")
        if original.width * original.height > 20_000_000:
            raise ValueError("输入图片像素超限")
        original.load()
        image = ImageOps.exif_transpose(original)
        before = {
            "width": image.width,
            "height": image.height,
            "format": original.format,
        }
        crop = request.get("crop")
        if crop is not None:
            if (
                not isinstance(crop, list)
                or len(crop) != 4
                or any(type(v) is not int for v in crop)
            ):
                raise ValueError("裁剪需使用 [left, top, right, bottom] 整数像素坐标")
            left, top, right, bottom = crop
            if not (
                0 <= left < right <= image.width and 0 <= top < bottom <= image.height
            ):
                raise ValueError("裁剪区域超出原图")
            image = image.crop(tuple(crop))
        width, height = request.get("width"), request.get("height")
        if width is not None or height is not None:
            if (
                type(width) is not int
                or type(height) is not int
                or not (
                    0 < width <= 8192
                    and 0 < height <= 8192
                    and width * height <= 20_000_000
                )
            ):
                raise ValueError("缩放需要明确宽高，最多 8192 边长、2000 万像素")
            image = image.resize((width, height), Image.Resampling.LANCZOS)
        form = request.get("format")
        if form not in {"png", "jpeg"}:
            raise ValueError("输出格式仅支持 png/jpeg；生成式绘图尚未接入")
        if form == "jpeg":
            rgba = image.convert("RGBA")
            background = Image.new("RGB", image.size, "white")
            background.paste(rgba, mask=rgba.getchannel("A"))
            image = background
        else:
            image = image.convert("RGBA")
        output = BytesIO()
        image.save(output, "JPEG" if form == "jpeg" else "PNG", quality=90)
        data = output.getvalue()
        if len(data) > 2 * 1024 * 1024:
            raise ValueError("输出超过 2 MiB，请缩小图片")
        return {
            "data": base64.b64encode(data).decode(),
            "mime": "image/" + form,
            "width": image.width,
            "height": image.height,
            "before": before,
        }


if __name__ == "__main__":
    try:
        if sys.platform != "win32":
            import resource

            resource.setrlimit(
                resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024)
            )
            resource.setrlimit(resource.RLIMIT_CPU, (7, 7))
        print(
            json.dumps(
                dict(transform(json.loads(Path(sys.argv[1]).read_bytes() if len(sys.argv)==2 else sys.stdin.buffer.read(8192))), ok=True)
            )
        )
    except Exception as error:
        print(json.dumps({"ok": False, "error": str(error)[:400]}))
