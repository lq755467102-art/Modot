"""Make a bounded image view from a file already resolved by project authorization."""

import base64
from io import BytesIO

from . import gcode_preview, image_evidence, workbench as W


GCODE_SUFFIXES = {".gcode", ".gco"}
SCHEMA = dict(
    type="object", additionalProperties=False,
    properties=dict(
        plane=dict(type="string", enum=["xy", "xz", "yz"], description="G-code 正投影平面，默认 xy"),
        object_id=dict(type="integer", minimum=0, description="G-code 可选打印对象编号，先从 availableObjects 核对"),
        layer=dict(type="string", enum=["all", "first", "last", "dense"], description="G-code 所有层、首层、末层或挤出路径最多的一层"),
        layer_index=dict(type="integer", minimum=0, description="G-code 指定层编号，从 layersByMoves 核对，可覆盖 layer"),
        align=dict(type="string", enum=["source", "principal"], description="G-code 倾斜文字用 principal 将轮廓长轴摆正"),
        rotation=dict(type="integer", minimum=-180, maximum=360, description="画面顺时针旋转角度"),
        mirror=dict(type="boolean", description="旋转后水平镜像，用于核对相反视向"),
        crop=dict(type="array", items=dict(type="number", minimum=0, maximum=1), minItems=4, maxItems=4,
                  description="旋转后以 0 到 1 表示的 [左,上,右,下] 裁切范围"),
    ),
)


def _parameters(value, gcode):
    if not isinstance(value, dict) or set(value) - SCHEMA["properties"].keys():
        raise ValueError("视图参数无效")
    plane = value.get("plane", "xy")
    layer = value.get("layer", "all")
    layer_index = value.get("layer_index")
    align = value.get("align", "source")
    object_id = value.get("object_id")
    rotation = value.get("rotation", 0)
    mirror = value.get("mirror", False)
    crop = value.get("crop")
    if (not gcode and set(value) & {"plane", "layer", "layer_index", "align", "object_id"}
            or plane not in ("xy", "xz", "yz") or layer not in ("all", "first", "last", "dense")
            or align not in ("source", "principal")
            or object_id is not None and (type(object_id) is not int or object_id < 0)
            or layer_index is not None and (type(layer_index) is not int or layer_index < 0)
            or type(rotation) is not int or not -180 <= rotation <= 360
            or type(mirror) is not bool
            or crop is not None and (not isinstance(crop, list) or len(crop) != 4
                                     or any(type(n) not in (int, float) or not 0 <= n <= 1 for n in crop)
                                     or not (crop[0] < crop[2] and crop[1] < crop[3]))):
        raise ValueError("视图平面、对象、旋转或裁切无效")
    return plane, layer, layer_index, align, object_id, rotation, mirror, crop


def read(path, view, *, expected_hash=None):
    is_gcode = path.suffix.lower() in GCODE_SUFFIXES
    try:
        from PIL import Image, ImageOps

        plane, layer, layer_index, align, object_id, rotation, mirror, crop = _parameters(view, is_gcode)
        if is_gcode:
            with path.open("rb") as stream:
                raw = stream.read(8 * 1024 * 1024 + 1)
            if len(raw) > 8 * 1024 * 1024:
                raise W.Problem("image_limit", "G-code 超过静态视图输入容量", 413)
            source_hash = W.digest(raw)
            if expected_hash and source_hash != expected_hash:
                raise W.Problem("resource_changed", "用于投影的资料已经变化", 409)
            image, geometry = gcode_preview.render(raw, plane, object_id, layer, layer_index, align)
            geometry.update(plane=plane, object_id=object_id, layer=layer, layer_index=layer_index, align=align)
        else:
            source = image_evidence.read(path, limit=2 * 1024 * 1024, expected_hash=expected_hash)
            source_hash = source["hash"]
            with Image.open(BytesIO(base64.b64decode(source["data_url"].split(",", 1)[1]))) as original:
                image = ImageOps.exif_transpose(original).convert("RGBA")
            background = Image.new("RGB", image.size, "white")
            background.paste(image, mask=image.getchannel("A"))
            image = background
            geometry = {}

        if rotation:
            image = image.rotate(-rotation, expand=True, resample=Image.Resampling.BICUBIC,
                                 fillcolor=255 if image.mode == "L" else (255, 255, 255))
        if mirror:
            image = ImageOps.mirror(image)
        if crop is not None:
            box = tuple(round(value * edge) for value, edge in zip(crop, (*image.size, *image.size)))
            if box[2] - box[0] < 2 or box[3] - box[1] < 2:
                raise ValueError("裁切区域至少需两像素")
            image = image.crop(box)
            if max(image.size) < 1500:
                scale = min(5, 1500 / max(image.size))
                image = image.resize(tuple(round(edge * scale) for edge in image.size), Image.Resampling.LANCZOS)
        image.thumbnail((3800, 3800), Image.Resampling.LANCZOS)

        stream = BytesIO()
        image.save(stream, format="PNG")
        data = stream.getvalue()
        if len(data) > image_evidence.MAX_BYTES:
            raise W.Problem("image_limit", "渲染图超过允许的体积，请缩小视图范围", 413)
    except ImportError as error:
        raise W.Problem("image_unavailable", "图片解码组件不可用", 503) from error
    except (ValueError, OverflowError) as error:
        raise W.Problem("view_invalid", "无法生成静态图片视图：" + str(error), 415) from error

    geometry.update(rotation=rotation, mirror=mirror, crop=crop)
    return dict(name=path.stem + "-view.png", path=str(path), bytes=len(data), width=image.width,
                height=image.height, mime="image/png", format="PNG", hash=W.digest(data),
                data_url=image_evidence.data_url(data, "image/png"), sourceHash=source_hash, view=geometry)
