"""Decode timestamped frames inside the fixed media parser sandbox."""
# 主要引用方包括 `parser_process.py`。

import base64
import hashlib
from io import BytesIO
import json
from pathlib import Path
import sys


MAX_PIXELS = 20_000_000
TILE_WIDTH, TILE_HEIGHT, LABEL_HEIGHT = 480, 270, 30


def frame_at(container, stream, seconds, origin):
    # Seek to the preceding keyframe, then find the frame displayed at the requested time.
    container.seek(int((seconds + origin) / stream.time_base), stream=stream, backward=True)
    previous = None
    for frame in container.decode(stream):
        if frame.time is None or frame.is_corrupt:
            continue
        timestamp = max(0.0, frame.time - origin)
        current = (frame, timestamp)
        if timestamp > seconds + 1e-7:
            return previous or current
        previous = current
    if previous is None:
        raise ValueError("指定时刻没有可定位的视频帧")
    return previous


def extract(request):
    import av
    from av.stream import Disposition
    from PIL import Image, ImageDraw, ImageFont

    with Path(request["path"]).open("rb") as stream:
        raw = stream.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != request["hash"]:
        raise ValueError("视频原件已变化")
    file_format = "mov" if request["format"] == "mp4" else "matroska"
    with av.open(BytesIO(raw), format=file_format) as container:
        stream = next(
            (item for item in container.streams.video if not item.disposition & Disposition.attached_pic),
            None,
        )
        if not stream or not stream.time_base:
            raise ValueError("文件没有带时间信息的视频轨")
        codec = stream.codec_context
        if not (0 < codec.width * codec.height <= MAX_PIXELS):
            raise ValueError("视频画面尺寸超出抽帧范围")
        codec.thread_count = 1
        origin = float((stream.start_time or 0) * stream.time_base)
        duration = (
            float(stream.duration * stream.time_base) if stream.duration is not None
            else container.duration / av.time_base if container.duration is not None else None
        )
        query = request.get("video")
        result = dict(ok=True, durationSeconds=duration, codec=codec.name)
        if query is None:
            return result
        start, end, count = query["start_seconds"], query["end_seconds"], query["frame_count"]
        if duration is not None and end > duration + 0.001:
            return dict(ok=False, code="video_range_outside", error=f"请求时间超出视频时长（{duration:.3f} 秒）")
        samples = [start + (end - start) * index / (count - 1) for index in range(count)] if count > 1 else [(start + end) / 2]
        columns = min(len(samples), 2)
        rows = (len(samples) + columns - 1) // columns
        height = TILE_HEIGHT + LABEL_HEIGHT
        sheet = Image.new("RGB", (TILE_WIDTH * columns, height * rows), "#17191b")
        draw = ImageDraw.Draw(sheet)
        font = ImageFont.load_default(size=16)
        frames = []
        for index, seconds in enumerate(samples):
            frame, timestamp = frame_at(container, stream, seconds, origin)
            if frame.width * frame.height > MAX_PIXELS:
                raise ValueError("视频帧尺寸超出抽帧范围")
            picture = frame.to_image()
            rotation = getattr(frame, "rotation", 0)
            if rotation:
                picture = picture.rotate(rotation, expand=True)
            picture.thumbnail((TILE_WIDTH, TILE_HEIGHT), Image.Resampling.LANCZOS)
            x = (index % columns) * TILE_WIDTH
            y = (index // columns) * height
            sheet.paste(picture, (x + (TILE_WIDTH - picture.width) // 2,
                                  y + (TILE_HEIGHT - picture.height) // 2))
            draw.text((x + 8, y + TILE_HEIGHT + 4), f"{seconds:.3f}s  (PTS {timestamp:.3f}s)", fill="white", font=font)
            frames.append(dict(requestedSeconds=round(seconds, 3), timestampSeconds=round(timestamp, 3)))
        output = BytesIO()
        sheet.save(output, "JPEG", quality=87)
        return dict(result, frames=frames, image=base64.b64encode(output.getvalue()).decode("ascii"))


def main():
    try:
        request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
        result = extract(request)
    except Exception as error:
        result = dict(ok=False, code="video_decode_failed", error=str(error)[:400] or "视频解码失败")
    sys.stdout.write(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
