"""Stage the pinned Windows components during the build; end-user setup works offline."""
import argparse
import os
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import zipfile

INSTALLER = Path(__file__).resolve().parent.parent / "installer"


def verified(directory, item):
    path = directory / item["file"]
    with path.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != item["sha256"]:
            raise ValueError(f"SHA-256 mismatch: {path.name}")
    return path


def stage(assets, target):
    pins_file = INSTALLER / "windows-components.json"
    pins = json.loads(pins_file.read_text(encoding="utf-8"))
    archive = verified(assets, pins["mingit"])
    msi = verified(assets, pins["dokan"])
    dokany, spdlog, gpl = [verified(assets, entry) for entry in pins["dokanSources"]]
    # The SHA-256 pins apply to complete, original upstream archives.
    with zipfile.ZipFile(archive) as tree:
        tree.extractall(target / "git")
    version = subprocess.check_output([target / "git/cmd/git.exe", "--version"], text=True).strip()
    if version != "git version " + pins["mingit"]["version"]:
        raise ValueError(f"Unexpected Git version: {version}")
    driver = target / "dokan"
    driver.mkdir()
    for source in (msi, pins_file, INSTALLER / "dokan-component.py"):
        shutil.copyfile(source, driver / source.name)
    notices = driver / "licenses"
    notices.mkdir()
    shutil.copyfile(gpl, notices / gpl.name)
    for path, entries in [(dokany, {"license.lgpl.txt": "Dokan-LGPL-3.0.txt", "license.mit.txt": "Dokan-MIT.txt"}),
                          (spdlog, {"LICENSE": "spdlog-LICENSE.txt"})]:
        with zipfile.ZipFile(path) as z:
            root = z.namelist()[0].split("/")[0]
            for relative, name in entries.items():
                (notices / name).write_bytes(z.read(f"{root}/{relative}"))


def pack_sources(assets, target, output):
    """Keep source downloads separate from the EXE and require complete package coverage."""
    pins = json.loads((INSTALLER / "windows-components.json").read_text(encoding="utf-8"))
    index = INSTALLER / "mingit-sources.json"
    sources = json.loads(index.read_text(encoding="utf-8"))
    inventory = target / "git/etc/package-versions.txt"
    expected = dict(line.split() for line in inventory.read_text().splitlines() if line.strip())
    covered = {}
    for row in sources:
        for package in row["packages"]:
            if package in covered:
                raise ValueError(f"Duplicate package: {package}")
            covered[package] = row["version"]
    if expected != covered:
        raise ValueError("MinGit source inventory differs from git/etc/package-versions.txt")
    partial = output.with_suffix(".partial")
    try:
        with zipfile.ZipFile(partial, "w", allowZip64=True) as bundle:
            for asset in [*pins["dokanSources"], pins["mingitBuild"]]:
                bundle.write(verified(assets, asset), asset["file"])
            for row in sources:
                bundle.write(verified(assets / "mingit-sources", row), "mingit/" + row["file"])
            for source in [index, INSTALLER / "windows-components.json", INSTALLER / "WINDOWS-COMPONENTS.md"]:
                bundle.write(source, source.name)
            bundle.write(inventory, "mingit/package-versions.txt")
            for path in sorted((target / "git").rglob("*")):
                if path.is_file() and (path == target / "git/LICENSE.txt" or
                                       any(part.lower() in {"license", "licenses"} for part in path.parts) or
                                       path.name.upper().startswith(("LICENSE", "COPYING", "NOTICE"))):
                    bundle.write(path, "notices/" + path.relative_to(target).as_posix())
            for path in (target / "dokan/licenses").iterdir():
                bundle.write(path, "notices/dokan/" + path.name)
        os.replace(partial, output)
    finally:
        partial.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("assets", type=Path)
    parser.add_argument("stage", type=Path)
    parser.add_argument("--source-bundle", type=Path)
    args = parser.parse_args()
    stage(args.assets, args.stage)
    if args.source_bundle:
        pack_sources(args.assets, args.stage, args.source_bundle)
