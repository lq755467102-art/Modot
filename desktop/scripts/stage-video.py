"""Stage the verified video wheel and matching notices for a Windows release."""

import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import sys
from zipfile import ZipFile


def verified_asset(directory, asset):
    path = directory / asset["file"]
    with path.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != asset["sha256"]:
        raise ValueError(f"Checksum mismatch for {path.name}")
    return path


def stage_video(lock_path, directory, stage, sources_output=None):
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    wheel = verified_asset(directory, lock["wheel"])
    sources = verified_asset(directory, lock["sources"])
    site = stage / "python" / "Lib" / "site-packages"
    metadata = f"av-{lock['version']}.dist-info"
    with ZipFile(wheel) as archive:
        members = archive.namelist()
        required = {"av/__init__.py", "av.libs/avcodec-63.dll", f"{metadata}/METADATA"}
        if not required.issubset(members):
            raise ValueError("The video wheel is missing required runtime files")
        for member in members:
            path = PurePosixPath(member)
            if (path.parts[0] not in {"av", "av.libs", metadata}
                    or ".." in path.parts or "\\" in member or ":" in member):
                raise ValueError(f"Unexpected video wheel member: {member}")
        archive.extractall(site)

    notices = stage / "licenses" / "video"
    with ZipFile(sources) as archive:
        entries = ["THIRD-PARTY-NOTICES.zh.md", "BUILD-SOURCE.zh.md"]
        entries += [
            member for member in archive.namelist()
            if member.startswith("licenses/toolchain/") and not member.endswith("/")
        ]
        if len(entries) == 2:
            raise ValueError("The matching source archive is missing toolchain notices")
        for member in entries:
            archive.extract(member, notices)
    if sources_output is not None:
        shutil.copyfile(sources, sources_output)


if __name__ == "__main__":
    stage_video(*(Path(value) for value in sys.argv[1:]))
