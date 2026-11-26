"""Static orthographic previews of deposited material in 3D printer G-code."""

from collections import Counter
from dataclasses import dataclass
import math
import re


PLANES = {"xy": (0, 1), "xz": (0, 2), "yz": (1, 2)}
COMMAND = re.compile(r"^(?:N\d+\s*)?([GM])\s*(\d+)(?![\d.])", re.IGNORECASE)
WORD = re.compile(r"([A-Za-z])\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))")
COMMENTS = re.compile(r"\([^)]*\)")
MAX_POINTS = 250_000


@dataclass
class Deposit:
    points: tuple
    object_id: int | None
    z: float


def _words(text):
    words = {axis.upper(): float(number) for axis, number in WORD.findall(text)}
    if any(not math.isfinite(value) or abs(value) > 1e7 for value in words.values()):
        raise ValueError("坐标超出投影范围")
    return words


def _arc(start, end, words, axes, clockwise):
    a, b = axes
    offsets = "IJK"
    if offsets[a] not in words and offsets[b] not in words:
        return None
    center_a = start[a] + words.get(offsets[a], 0)
    center_b = start[b] + words.get(offsets[b], 0)
    radius = math.hypot(start[a] - center_a, start[b] - center_b)
    if radius < 1e-8 or abs(math.hypot(end[a] - center_a, end[b] - center_b) - radius) > max(.02, radius * .01):
        return None
    first = math.atan2(start[b] - center_b, start[a] - center_a)
    last = math.atan2(end[b] - center_b, end[a] - center_a)
    turn = -((first - last) % math.tau or math.tau) if clockwise else (last - first) % math.tau or math.tau
    steps = min(96, max(2, math.ceil(abs(turn) * 16)))
    points = [start]
    for step in range(1, steps):
        fraction = step / steps
        point = [x + (y - x) * fraction for x, y in zip(start, end)]
        point[a] = center_a + radius * math.cos(first + turn * fraction)
        point[b] = center_b + radius * math.sin(first + turn * fraction)
        points.append(tuple(point))
    return (*points, end)


def deposits(data):
    pos = (0., 0., 0.)
    origin = [0., 0., 0.]
    extrusion = 0.
    relative = relative_e = False
    unit = 1.
    plane = PLANES["xy"]
    object_id = None
    names = {}
    paths = []
    approximated = points_count = 0
    for line in data.decode("utf-8", errors="replace").splitlines():
        line = COMMENTS.sub("", line.partition(";")[0]).strip()
        match = COMMAND.match(line)
        if not match:
            continue
        code = match[1].upper() + str(int(match[2]))
        args = line[match.end():].strip()
        if code in {"G90", "G91"}:
            relative = relative_e = code == "G91"
        elif code in {"M82", "M83"}:
            relative_e = code == "M83"
        elif code in {"G20", "G21"}:
            unit = 25.4 if code == "G20" else 1.
        elif code in {"G17", "G18", "G19"}:
            plane = {"G17": (0, 1), "G18": (2, 0), "G19": (1, 2)}[code]
        elif code == "M486":
            if args.upper().startswith("A") and object_id is not None and object_id >= 0:
                names[object_id] = args[1:].strip()[:80]
            elif args.upper().startswith("S"):
                words = _words(args)
                if "S" in words:
                    object_id = int(words["S"])
        elif code in {"G0", "G1", "G2", "G3", "G92"}:
            words = {key: value * unit for key, value in _words(args).items()}
            if code == "G92":
                for i, axis in enumerate("XYZ"):
                    if axis in words:
                        origin[i] = pos[i] - words[axis]
                extrusion = words.get("E", extrusion)
                continue
            end = tuple(pos[i] if axis not in words else words[axis] + (pos[i] if relative else origin[i])
                        for i, axis in enumerate("XYZ"))
            e = words.get("E")
            if e is not None:
                delta = e if relative_e else e - extrusion
                extrusion += delta
                if delta > 1e-7 and (pos != end or code in {"G2", "G3"}):
                    path = _arc(pos, end, words, plane, code == "G2") if code in {"G2", "G3"} else (pos, end)
                    if path is None:
                        approximated += 1
                        path = (pos, end)
                    points_count += len(path)
                    if points_count > MAX_POINTS:
                        raise ValueError("沉积路径超过静态预览容量")
                    paths.append(Deposit(path, object_id, round(end[2], 5)))
            pos = end
    counts = Counter(path.object_id for path in paths if path.object_id is not None and path.object_id >= 0)
    if counts:
        paths = [path for path in paths if path.object_id in counts]
    objects = [dict(id=ident, name=names.get(ident, ""), depositMoves=count)
               for ident, count in sorted(counts.items())]
    return paths, objects, approximated


def _projection(paths, axes, align):
    x, y = axes
    paths = [[(point[x], point[y]) for point in path.points] for path in paths]
    points = [point for path in paths for point in path]
    angle = 0.
    if align == "principal":
        count = len(points)
        mx = sum(x for x, _ in points) / count
        my = sum(y for _, y in points) / count
        cross = sum((x - mx) * (y - my) for x, y in points)
        difference = sum((x - mx) ** 2 - (y - my) ** 2 for x, y in points)
        angle = math.atan2(2 * cross, difference) / 2
        c, s = math.cos(angle), math.sin(angle)
        paths = [[(x * c + y * s, -x * s + y * c) for x, y in path] for path in paths]
    return paths, round(math.degrees(angle), 3)


def render(data, plane, object_id=None, layer="all", layer_index=None, align="source"):
    from PIL import Image, ImageDraw

    paths, objects, approximated = deposits(data)
    if object_id is not None:
        paths = [path for path in paths if path.object_id == object_id]
    if not paths:
        raise ValueError("没有可投影的正挤出路径，请核对对象或刀路类型")
    counts = Counter(path.z for path in paths)
    heights = sorted(counts)
    z = None
    if layer_index is not None:
        if layer_index >= len(heights):
            raise ValueError("指定层编号不在资料中")
        z = heights[layer_index]
    elif layer == "dense":
        z = max(heights, key=counts.__getitem__)
    elif layer != "all":
        z = heights[0 if layer == "first" else -1]
    if z is not None:
        paths = [path for path in paths if path.z == z]
    projected, angle = _projection(paths, PLANES[plane], align)
    points = [point for path in projected for point in path]
    left, right = min(x for x, _ in points), max(x for x, _ in points)
    low, high = min(y for _, y in points), max(y for _, y in points)
    dx, dy = right - left, high - low
    scale = 3440 / max(dx, dy, 1e-4)
    margin = 32
    image = Image.new("L", (round(dx * scale) + margin * 2, round(dy * scale) + margin * 2), 255)
    draw = ImageDraw.Draw(image)
    width = max(2, min(20, round(scale * .17)))
    for path in projected:
        hit = [(margin + (x - left) * scale, margin + (high - y) * scale) for x, y in path]
        draw.line(hit, fill=0, width=width, joint="curve")

    layers = [dict(index=i, zMm=height, depositMoves=counts[height]) for i, height in enumerate(heights)]
    return image, dict(availableObjects=objects, selectedMoves=len(paths), layerCount=len(heights),
                       layersByMoves=sorted(layers, key=lambda row: -row["depositMoves"])[:10], selectedZMm=z,
                       principalAxisDegrees=angle, approximatedArcCount=approximated)
