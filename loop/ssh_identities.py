"""User-owned SSH identities for a failed import. Only public metadata reaches the UI."""
# 主要引用方包括 `projects.py` 和 `repository_import.py`。

import base64
import binascii
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import uuid

from . import evidence, projects, workbench as W
from .filelock import FileLock

RECOVERABLE = {"ssh_access_denied", "ssh_repository_unavailable", "ssh_identity_unavailable",
               "ssh_identity_locked", "ssh_host_untrusted", "import_network", "ssh_repository_path"}


def pending(session_id, sv):
    thread = projects.conversation(session_id)
    if any(thread.get(k) for k in ("archived", "readonlyEvidence", "subtaskOf", "scheduledRun")):
        return None
    directory = evidence.directory(session_id, sv)
    with evidence.projection(directory) as (db, state):
        if state.get("catchingUp"):
            raise W.Problem("evidence_indexing", "导入回执仍在读取，请稍后重试", 425)
        row = db.execute("SELECT data FROM actions WHERE tool='import_private_repository' AND role='executor' "
                         "ORDER BY position DESC LIMIT 1").fetchone()
        if not row:
            return None
        record = json.loads(row[0])
        if record.get("state") != "failed" or (record.get("impact") or {}).get("code") not in RECOVERABLE:
            return None
        evidence._verify_source(directory, record, db)
        args = record.get("input") or {}
        from .repository_ssh import source
        url, _, default_name, _, _ = source(args.get("url"))
        return dict(actionId=record["id"], runId=record["runId"], code=record["impact"]["code"],
                    url=url, name=args.get("name") or default_name)


def _location(path):
    return path.absolute() == path.resolve() and not str(path).startswith("\\\\")


def _public(path):
    if path.suffix != ".pub" or not _location(path) or not path.is_file():
        raise ValueError("not a local public key")
    with path.open("rb") as handle:
        line = handle.read(16385)
    if len(line) > 16384:
        raise ValueError("oversize public key")
    kind, encoded, *_ = line.strip().split(None, 2)
    if not re.fullmatch(rb"(?:ssh-(?:rsa|ed25519)|ecdsa-sha2-nistp(?:256|384|521)|sk-(?:ssh-ed25519|ecdsa-sha2-nistp256)@openssh.com)", kind):
        raise ValueError("unsupported public key")
    blob = base64.b64decode(encoded, validate=True)
    if len(blob) < 16 or int.from_bytes(blob[:4], "big") != len(kind) or blob[4:4+len(kind)] != kind:
        raise ValueError("invalid public key")
    sibling = path.with_suffix("")
    if not _location(sibling):
        raise ValueError("linked private key")
    fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    return dict(id=hashlib.sha256((str(path) + "\n" + fingerprint).encode()).hexdigest(),
                label=path.stem, fingerprint=fingerprint, publicKey=(kind + b" " + encoded).decode())


def _root():
    return Path.home() / ".ssh"


def _options():
    root = _root()
    if not _location(root):
        return
    for path in (*root.glob("*.pub"), *(root / "modot").glob("*/identity.pub")):
        try:
            record = _public(path)
        except (OSError, ValueError, binascii.Error):
            continue
        if path.parent.parent == root / "modot":
            record["label"] = "Modot · " + path.parent.name[:8]
        yield path, record


def _file(session_id):
    return W.home() / "security" / "ssh-identities" / (W.digest(session_id.encode()) + ".json")


def identity(destination):
    """Acquire the selected identity only for this conversation, repository and target."""
    if not destination.session_id:
        return None
    row = W.read(_file(destination.session_id))
    if (row.get("url"), row.get("target"), row.get("project")) != (
            destination.url, str(destination.target), destination.project_id):
        return None
    try:
        public_path = Path(row["publicPath"])
        record = _public(public_path)
        if record["id"] != row["identityId"]:
            raise ValueError("changed public key")
        private_path = public_path.with_suffix("")
        # OpenSSH can also select an agent-held key by its public file.
        return dict(file=str(private_path if private_path.is_file() else public_path),
                    fingerprint=record["fingerprint"], id=record["id"])
    except (OSError, KeyError, ValueError) as error:
        raise W.Problem("ssh_identity_unavailable", "已选 SSH 身份已移动或改变，请重新选择", 409) from error


def _create():
    from .repository_ssh import openssh
    root = _root()
    managed = root / "modot"
    if not _location(managed):
        raise W.Problem("ssh_identity_unavailable", "SSH 身份目录不能使用链接", 403)
    managed.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = managed / uuid.uuid4().hex
    directory.mkdir(mode=0o700)
    private = directory / "identity"
    try:
        result = subprocess.run([str(openssh("ssh-keygen")), "-q", "-t", "ed25519", "-N", "",
                                 "-C", "Modot", "-f", str(private)], cwd=directory, timeout=20,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            raise OSError("keygen failed")
        pub = private.with_suffix(".pub")
        return pub, _public(pub)
    except (OSError, ValueError, subprocess.SubprocessError, W.Problem) as error:
        # This unique directory was just allocated by this call.
        for owned in (private, private.with_suffix(".pub")):
            owned.unlink(missing_ok=True)
        directory.rmdir()
        raise W.Problem("ssh_keygen_failed", "本机未能创建 SSH 身份，请核对 OpenSSH 和身份目录权限", 409) from error


def route(method, session_id, body, sv):
    issue = pending(session_id, sv)
    if method == "GET":
        return 200, dict(issue=issue)
    if method != "POST" or not issue or body.get("actionId") != issue["actionId"]:
        raise W.Problem("ssh_recovery_stale", "这次导入已变化，请重新打开当前回执", 409)
    if body.get("operation") == "list":
        return 200, dict(identities=[{k: v for k, v in record.items() if k != "publicKey"}
                                     for _, record in _options()])
    if body.get("operation") not in {"create", "select"} or body.get("confirm") is not True:
        raise W.Problem("confirmation", "请选择本次导入要使用的本机身份", 403)
    from .repository_import import Destination
    thread = projects.conversation(session_id)
    dest = Destination(issue["url"], issue["name"], private=True, session_id=session_id, project_id=thread.get("projectId"))
    state = _file(session_id)
    state.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with FileLock(state.with_suffix(".lock")):
        if pending(session_id, sv) != issue:
            raise W.Problem("ssh_recovery_stale", "导入回执已更新", 409)
        dest.check()
        prior = W.read(state)
        if body["operation"] == "create":
            if prior.get("generatedFor") == issue["actionId"]:
                path = Path(prior["publicPath"])
                try:
                    record = _public(path)
                except (ValueError, OSError) as error:
                    raise W.Problem("ssh_identity_unavailable", "本次创建的身份已变化，请选择已有身份", 409) from error
            else:
                path, record = _create()
        else:
            selected = next(((path, rec) for path, rec in _options() if rec["id"] == body.get("identityId")), None)
            if selected is None:
                raise W.Problem("ssh_identity_unavailable", "本机未找到所选公钥，请刷新", 409)
            path, record = selected
        W.atomic_json(state, dict(identityId=record["id"], publicPath=str(path), url=dest.url,
                                  target=str(dest.target), project=dest.project_id,
                                  generatedFor=issue["actionId"] if body["operation"] == "create" else None))
    return 200, dict(identity=record)
