"""VS Code application slot archive inspection. Only the approved host expands the checked original."""
# 主要引用方包括 `app_testing.py` 和 `app_formatting.py`。

import io
import re
import stat
import zipfile
from . import plugins, workbench as W

MAX_VSIX = 2 * 1024 * 1024
TESTING_CATEGORY = "Testing"
NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,100}")


def archive_info(raw, destination=None, *, category=TESTING_CATEGORY, maximum=MAX_VSIX):
    """Validate the original on install and again before deploying just its extension directory."""
    if not 0 < len(raw) <= maximum:
        raise W.Problem("vsix_capacity", f"此类扩展原包上限为 {maximum // (1024 * 1024)} MiB。", 413)
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            if len(entries) > 4096 or sum(e.file_size for e in entries) > 48 * 1024 * 1024:
                raise ValueError("归档超过文件数或解压总量限制")
            seen = set()
            for entry in entries:
                parts = entry.filename.rstrip("/").split("/")
                if (
                    any(
                        not part
                        or part in {".", ".."}
                        or part.endswith((" ", "."))
                        or re.search(r'[\\:<>"|?*\x00-\x1f]', part)
                        or re.fullmatch(r"(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part, re.I)
                        for part in parts
                    )
                    or len(entry.filename) > 300
                    or entry.flag_bits & 1
                    or stat.S_IFMT(entry.external_attr >> 16) not in {0, stat.S_IFDIR, stat.S_IFREG}
                    or entry.file_size > 12 * 1024 * 1024
                    or entry.filename.rstrip("/").casefold() in seen
                ):
                    raise ValueError("归档包含不支持的文件或路径")
                seen.add(entry.filename.rstrip("/").casefold())
                if destination is not None and parts[0] == "extension" and not entry.is_dir():
                    output = destination.joinpath(*parts[1:])
                    if not output.parent.resolve().is_relative_to(destination.resolve()):
                        raise ValueError("文件超出扩展目录")
                    output.parent.mkdir(parents=True, exist_ok=True)
                    with output.open("xb") as target:
                        target.write(archive.read(entry))
            if (
                "extension.vsixmanifest" not in archive.namelist()
                or archive.getinfo("extension/package.json").file_size > 512 * 1024
            ):
                raise ValueError("缺少 VSIX 清单或清单过大")
            manifest = plugins.packages.parse_json(
                archive.read("extension/package.json"), "extension/package.json"
            )
            if not isinstance(manifest, dict):
                raise ValueError("清单无效")
            publisher, name, version = (manifest.get(k) for k in ("publisher", "name", "version"))
            main = manifest.get("main")
            if (
                not all(isinstance(v, str) and NAME.fullmatch(v) for v in (publisher, name))
                or not isinstance(version, str)
                or not re.fullmatch(r"[\w][\w.+-]{0,63}", version, re.ASCII)
                or not isinstance(main, str)
                or not isinstance(manifest.get("categories"), list)
                or category not in manifest["categories"]
                or manifest.get("extensionDependencies")
                or manifest.get("extensionPack")
                or not isinstance(manifest.get("engines"), dict)
                or not isinstance(manifest["engines"].get("vscode"), str)
            ):
                raise ValueError(f"需要独立的 VS Code {category} 类 Node 扩展")
            entrypoint = "extension/" + main.removeprefix("./")
            if not any(candidate in archive.namelist() for candidate in (entrypoint, entrypoint + ".js")):
                raise ValueError("清单中的扩展入口缺失")
            display = manifest.get("displayName")
            return dict(
                extensionId=(publisher + "." + name).lower(),
                version=version,
                sha256=W.digest(raw),
                name=display[:120] if isinstance(display, str) and not display.startswith("%") else name,
            )
    except (ValueError, KeyError, OSError, RuntimeError, zipfile.BadZipFile, NotImplementedError) as exc:
        raise W.Problem("invalid_testing_vsix", "无法装载扩展。" + str(exc)[:180]) from exc
