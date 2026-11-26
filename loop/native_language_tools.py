"""Discover existing editor services, or install just the missing components in a private prefix."""
# 主要引用方包括 `workroom_language.py`。
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
from urllib.request import Request, urlopen
import zipfile

from . import workbench as W

CLANG_RELEASE = "23.1.0"
CLANG_ZIP = f"https://github.com/clangd/clangd/releases/download/{CLANG_RELEASE}/clangd-windows-{CLANG_RELEASE}.zip"
CLANG_SHA256 = "23412a240756a162e7b98a282f36aa2a23a88db5ce16a0cbc4fef7253768c810"
RUST_RELEASE = "1.100.0"
RUST_DIST = "https://static.rust-lang.org/dist/"
RUST_TARGET = "x86_64-pc-windows-msvc"
PYRIGHT_VERSION = "1.1.414"
PYRIGHT_SHA256 = "bf5f473f6167c0d14175492c3263d783b4489a6956e1c06c18e15228e3a3fa42"
BIN = ".exe" if os.name == "nt" else ""


def disk_path(path):
    """The embedded Windows Python may not opt in to unprefixed long paths."""
    value = str(path.absolute())
    if os.name != "nt" or value.startswith("\\\\?\\"):
        return path
    if value.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + value[2:])
    return Path("\\\\?\\" + value)


def probe(executable, *args):
    if not executable or not Path(executable).is_file():
        return ""
    try:
        result = subprocess.run(
            [str(executable), *args], cwd=tempfile.gettempdir(), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5, check=False,
            env={**os.environ, "RUSTUP_AUTO_INSTALL": "0"},
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return result.stdout.decode("utf-8", "replace").strip() if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def candidates(name, extra=()):
    dirs = [*extra, *(Path(p.strip('"')) for p in os.get_exec_path() if p)]
    if os.name == "nt" and name == "clangd":
        for key in ("ProgramFiles", "ProgramFiles(x86)"):
            if prefix := os.environ.get(key):
                dirs.append(Path(prefix) / "LLVM/bin")
                dirs.extend((Path(prefix) / "Microsoft Visual Studio").glob("*/*/VC/Tools/Llvm/x64/bin"))
    if name != "clangd":
        dirs.append(Path(os.environ.get("CARGO_HOME") or Path.home() / ".cargo") / "bin")
    seen = set()
    for directory in dirs:
        # Do not execute a program found relative to a project or to the working directory.
        if not directory.is_absolute():
            continue
        exe = directory / (name + BIN)
        if exe not in seen and exe.is_file():
            seen.add(exe)
            yield exe


def find_version(name, extra=()):
    for exe in candidates(name, extra):
        match = re.search(r"(?im)^(?:Apple )?" + re.escape(name) + r"(?: version)?\s+([0-9][^\s]*)", probe(exe, "--version"))
        if match:
            return str(exe), match.group(1)
    return "", ""


def download(url, target, digest, limit):
    if not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValueError("invalid download digest")
    count, checksum = 0, hashlib.sha256()
    with urlopen(Request(url, headers={"User-Agent": "Modot-workroom"}), timeout=45) as source, target.open("xb") as out:
        while chunk := source.read(1024 * 1024):
            count += len(chunk)
            if count > limit:
                raise ValueError("component exceeds download limit")
            checksum.update(chunk)
            out.write(chunk)
    if checksum.hexdigest() != digest:
        raise W.Problem("language_integrity", "下载内容未通过官方校验，请重新安装。", 422)


def relative_parts(name):
    parts = PurePosixPath(name).parts
    if not parts or name.startswith("/") or "\\" in name or any(p in (".", "..") or ":" in p for p in parts):
        raise ValueError("invalid archive member")
    return parts


def copy_member(source, dest, size):
    if size < 0 or size > 1024**3:
        raise ValueError("invalid component member size")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with source, dest.open("wb") as out:
        shutil.copyfileobj(source, out, 1024 * 1024)


def supported_download():
    if os.name != "nt" or platform.machine().lower() not in ("amd64", "x86_64"):
        raise W.Problem("language_platform", "请先为此平台安装本机语言服务，再从工作间启用。", 422)


def install_clang(stage):
    exe, version = find_version("clangd")
    if exe:
        return {"server": exe, "source": "system", "version": version}
    supported_download()
    archive = stage / "clangd.zip"
    download(CLANG_ZIP, archive, CLANG_SHA256, 256 * 1024**2)
    with zipfile.ZipFile(archive) as zipped:
        for entry in zipped.infolist():
            parts = relative_parts(entry.filename)
            if entry.is_dir() or len(parts) < 2 or parts[0] != "clangd_" + CLANG_RELEASE:
                continue
            if stat.S_ISLNK(entry.external_attr >> 16):
                raise ValueError("unexpected link in clangd archive")
            copy_member(zipped.open(entry), stage.joinpath(*parts[1:]), entry.file_size)
    archive.unlink()
    server = "bin/clangd.exe"
    if not probe(stage / server, "--version").startswith("clangd version "):
        raise ValueError("installed clangd is unavailable")
    return {"server": server, "source": "managed", "version": CLANG_RELEASE}


def rust_release_location(version, sysroot):
    channel = version.split("-", 1)[-1].split(".")[0] if "-" in version else ""
    folder = ""
    if channel:
        # Rustup records the exact distribution date, including for older nightly/beta toolchains.
        installed = sysroot / "lib/rustlib/multirust-channel-manifest.toml"
        date = None
        if installed.is_file():
            with installed.open("rb") as manifest:
                date = re.search(rb'(?m)^date\s*=\s*"(\d{4}-\d{2}-\d{2})"', manifest.read(2048))
        if channel not in {"beta", "nightly"} or not date:
            raise W.Problem("language_components", "请为当前自定义 Rust 工具链安装缺少的组件，然后重试。", 422)
        folder, version = date.group(1).decode("ascii") + "/", channel
    return RUST_DIST + folder, version


def rust_component(stage, base_url, version, name):
    target = "" if name == "rust-src" else "-" + RUST_TARGET
    url = f"{base_url}{name}-{version}{target}.tar.xz"
    with urlopen(url + ".sha256", timeout=30) as response:
        digest = response.read(256).decode("ascii").partition(" ")[0].strip()
    packed = stage / "component.tar.xz"
    download(url, packed, digest, 512 * 1024**2)
    with tarfile.open(packed, mode="r|xz") as tar:
        for item in tar:
            parts = relative_parts(item.name)
            if len(parts) < 4 or parts[2] not in ("bin", "lib", "libexec", "share") or item.isdir():
                continue
            if not item.isfile():
                raise ValueError("unexpected link in Rust component")
            copy_member(tar.extractfile(item), stage.joinpath(*parts[2:]), item.size)
    packed.unlink()


def install_rust(stage):
    sysroot, release = None, RUST_RELEASE
    for rustc in candidates("rustc"):
        root = Path(probe(rustc, "--print", "sysroot"))
        version = re.match(r"rustc (\d+\.\d+\.\d+(?:-[\w.-]+)?)\s", probe(rustc, "--version"))
        if root.is_absolute() and version and (root / "bin" / ("rustc" + BIN)).is_file():
            sysroot, release = root, version.group(1)
            break
    preferred = [sysroot / "bin"] if sysroot else []
    server, version = find_version("rust-analyzer", preferred)
    cargo, _ = find_version("cargo", preferred)
    src_rel = "lib/rustlib/src/rust/library"
    src = Path(os.environ.get("RUST_SRC_PATH") or (sysroot / src_rel if sysroot else src_rel))
    local_src = src.is_absolute() and (src / "core/src/lib.rs").is_file()
    missing = []
    if not server:
        missing.append("rust-analyzer")
    if not sysroot:
        missing += ["rustc", "rust-std"]
    if not cargo:
        missing.append("cargo")
    if not local_src:
        missing.append("rust-src")
    if missing:
        supported_download()
        base_url, version_name = rust_release_location(release, sysroot)
        for component in missing:
            rust_component(stage, base_url, version_name, component)
    server_path = server or "bin/rust-analyzer" + BIN
    cargo_path = cargo or "bin/cargo" + BIN
    rustc_path = str(sysroot / "bin" / ("rustc" + BIN)) if sysroot else "bin/rustc" + BIN
    if (not probe(stage / server_path, "--version") or not probe(stage / cargo_path, "--version")
            or not probe(stage / rustc_path, "--version") or not (stage / (src if local_src else src_rel) / "core/src/lib.rs").is_file()):
        raise ValueError("Rust language components are incomplete")
    return {"server": server_path, "cargo": cargo_path, "rustc": rustc_path,
            "sysroot": str(sysroot) if sysroot else ".", "sources": str(src) if local_src else src_rel,
            "version": version or release, "source": "system" if not missing else "mixed" if (server or sysroot or cargo or local_src) else "managed"}


def installed_pyright_packages():
    """Use installed npm packages without running a shell shim or searching the checkout."""
    directories = [Path(value.strip('"')) for value in os.get_exec_path() if value]
    if prefix := os.environ.get("npm_config_prefix"):
        directories.extend((Path(prefix), Path(prefix) / "bin"))
    if os.name == "nt" and (appdata := os.environ.get("APPDATA")):
        directories.append(Path(appdata) / "npm")
    for directory in dict.fromkeys(directories):
        if not directory.is_absolute() or directory.name == ".bin":
            continue
        shim = directory / "pyright-langserver"
        if shim.is_symlink():
            yield shim.resolve().parent
        yield directory / "node_modules/pyright"
        if directory.name == "bin":
            yield directory.parent / "lib/node_modules/pyright"


def pyright_version(package):
    required = ("langserver.index.js", "dist/pyright-langserver.js", "dist/typeshed-fallback/stdlib/builtins.pyi")
    if not all((package / name).is_file() for name in required):
        return None
    try:
        manifest = json.loads((package / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(manifest, dict) and manifest.get("name") == "pyright" and isinstance(manifest.get("version"), str):
        return manifest["version"]
    return None


def install_pyright(stage):
    for package in installed_pyright_packages():
        if version := pyright_version(package):
            return {"server": str((package / "langserver.index.js").resolve()), "version": version, "source": "system"}
    archive = stage / "pyright.tgz"
    download(f"https://registry.npmjs.org/pyright/-/pyright-{PYRIGHT_VERSION}.tgz", archive, PYRIGHT_SHA256, 16 * 1024**2)
    with tarfile.open(archive, "r|gz") as tar:
        for member in tar:
            parts = relative_parts(member.name)
            if member.isdir() or len(parts) < 2 or parts[0] != "package":
                continue
            if not member.isfile():
                raise ValueError("unexpected link in Pyright package")
            if member.name.endswith(".map"):
                continue
            copy_member(tar.extractfile(member), stage.joinpath(*parts[1:]), member.size)
    archive.unlink()
    if pyright_version(stage) != PYRIGHT_VERSION:
        raise ValueError("Pyright installation is incomplete")
    return {"server": "langserver.index.js", "source": "managed", "version": PYRIGHT_VERSION}


def prepare(language, stage):
    try:
        return {"cpp": install_clang, "rust": install_rust, "python": install_pyright}[language](stage)
    except (OSError, ValueError, KeyError, tarfile.TarError, zipfile.BadZipFile) as error:
        raise W.Problem("language_install", f"未能准备语言服务：{error}", 503) from error
