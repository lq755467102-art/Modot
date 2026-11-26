"""Check a package's catalog declarations offline, using Modot's existing package inspector."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from loop import plugin_packages, workbench
from loop.plugin_contributions import PREVIEW, ManifestError, checked_json, add_preview

MANIFESTS = {
    "agent-plugins": "plugin.json",
    "codex": ".codex-plugin/plugin.json",
    "claude": ".claude-plugin/plugin.json",
    "cursor-common-components": ".cursor-plugin/plugin.json",
    "dsh-native": "package.json",
}


def check_mcp_file(package, manifest):
    kind = package["format"]
    if kind == "agent-plugins":
        relative = "mcp.json"
    elif kind == "dsh-native":
        return
    elif manifest.get("mcpServers"):
        root = Path(package["root"])
        relative = plugin_packages.inside(root, manifest["mcpServers"]).relative_to(root).as_posix()
        if relative not in package["files"]:
            raise ManifestError("missing_mcp", "mcpServers", f"找不到已声明的包内配置 {relative}。")
    elif kind == "claude":
        relative = ".mcp.json"
    else:
        return
    if relative in package["files"]:
        checked_json(package, relative)


def component_rows(package):
    """Names belong to their component type and the enclosing package fingerprint."""
    rows = []
    for skill in package["skills"]:
        rows.append({"kind": "skill", "localId": skill["name"],
                     "status": "unsupported" if skill["unsupported"] else "recognized"})
    for server in package["servers"]:
        rows.append({"kind": "mcp", "localId": server["name"],
                     "status": "recognized" if server["supported"] else "unsupported"})
    for key, kind in (("contextStores", "context-store"), ("runtimeFacts", "runtime-fact"), ("workers", "worker")):
        rows.extend({"kind": kind, "localId": item["name"], "status": "recognized"}
                    for item in package.get(key, []))
    if package.get("memoryProvider"):
        rows.append({"kind": "memory-provider", "localId": package["memoryProvider"]["adapter"],
                     "status": "recognized"})
    for hook in package.get("hooks", []):
        # Legacy hook declarations expose a descriptor, but no stable author-supplied ID.
        rows.append({"kind": "hook", "event": hook["event"], "matcher": hook["matcher"],
                     "entry": hook["entry"], "status": "recognized"})
    return rows


def check_package(directory):
    report = {"path": str(directory), "contributions": [], "diagnostics": []}
    field = "package"
    try:
        # Keep a final symlink visible to the inspector. The inspector validates the root.
        package = plugin_packages.inspect(str(Path(directory).absolute()))
        report.update({key: package[key] for key in ("name", "version", "format", "fingerprint")})
        field = MANIFESTS[package["format"]]
        manifest = checked_json(package, field)
        check_mcp_file(package, manifest)
        report["contributions"] = component_rows(package)
        report["diagnostics"].extend({"level": "warning", "code": "host_unsupported",
                                      "field": field, "message": reason} for reason in package["unsupported"])
        if package["format"] == "agent-plugins":
            add_preview(manifest, report)
        elif isinstance(manifest.get("extensions"), dict) and PREVIEW in manifest["extensions"]:
            report["diagnostics"].append({"level": "warning", "code": "catalog_format", "field": field,
                                          "message": "46 的试验声明只从包根 plugin.json 读取。"})
    except ManifestError as exc:
        report["diagnostics"].append({"level": "error", "code": exc.code, "field": exc.field, "message": str(exc)})
    except workbench.Problem as exc:
        report["diagnostics"].append({"level": "error", "code": exc.code, "field": field, "message": exc.message})
    except (OSError, ValueError, TypeError, AttributeError, RecursionError) as exc:
        report["diagnostics"].append({"level": "error", "code": "manifest_read", "field": field, "message": str(exc)})
    report["ok"] = not any(item["level"] == "error" for item in report["diagnostics"])
    return report


def main():
    parser = argparse.ArgumentParser(description="离线核对插件声明与内容指纹。结果仅描述包内容。")
    parser.add_argument("directories", metavar="DIR", nargs="+", help="一个或多个插件包根目录")
    parser.add_argument("--json", action="store_true", help="输出带诊断代码的 JSON")
    args = parser.parse_args()
    packages = [check_package(item) for item in args.directories]
    passed = all(item["ok"] for item in packages)
    if args.json:
        print(json.dumps({"ok": passed, "packages": packages}, ensure_ascii=False, indent=2))
    else:
        labels = {"recognized": "宿主可识别", "unsupported": "需适配", "declared-only": "仅有声明，等待页面宿主"}
        for item in packages:
            print(f"{'通过' if item['ok'] else '失败'}  {item['path']}")
            if "fingerprint" in item:
                print(f"  {item['name']}  {item['version']}  {item['format']}  {item['fingerprint']}")
            if "packageKey" in item:
                print(f"  作者声明的包标识  {item['packageKey']}")
            for row in item["contributions"]:
                name = row.get("localId") or f"{row['event']} {row['matcher']} {row['entry']}"
                print(f"  {row['kind']}  {name}  {labels[row['status']]}")
            for issue in item["diagnostics"]:
                print(f"  [{issue['level']}/{issue['code']}] {issue['field']}  {issue['message']}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
