"""Fetch digest-pinned inputs on the build machine. Never shipped as an end-user install step."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import shutil
import sys
from tempfile import NamedTemporaryFile
from urllib.request import Request, urlopen

INSTALLER = Path(__file__).resolve().parent.parent / "installer"


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def fetch(entry):
    directory, asset = entry
    path = directory / asset["file"]
    if path.exists() and digest(path) == asset["sha256"]:
        return
    directory.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=directory, delete=False) as output:
        temporary = Path(output.name)
    try:
        request = Request(asset["url"], headers={"User-Agent": "Modot-release-build"})
        with urlopen(request, timeout=120) as response, temporary.open("wb") as output:
            shutil.copyfileobj(response, output, length=1024*1024)
        if digest(temporary) != asset["sha256"]:
            raise ValueError(f"SHA-256 mismatch: {asset['file']}")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def main(directory):
    pins = json.loads((INSTALLER / "windows-components.json").read_text(encoding="utf-8"))
    sources = json.loads((INSTALLER / "mingit-sources.json").read_text(encoding="utf-8"))
    assets = [(directory, row) for row in [pins["mingit"], pins["dokan"], pins["mingitBuild"], *pins["dokanSources"]]]
    assets += [(directory / "mingit-sources", row) for row in sources]
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(fetch, assets))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: fetch-windows-components.py <build-inputs-directory>")
    main(Path(sys.argv[1]))
