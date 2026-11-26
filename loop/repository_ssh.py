"""Read one approved repository over SSH. Git objects are checked out in the import sandbox."""
# 主要引用方包括 `repository_import.py` 和 `ssh_identities.py`。

import os
from pathlib import Path
import re
import subprocess
import threading
from urllib.parse import urlsplit

from . import workbench as W
from .process_family import Family


def source(value):
    """Keep scp-relative paths and SSH URL absolute paths distinct."""
    if not isinstance(value, str) or len(value) > 1024:
        raise W.Problem("invalid_repository_url", "请填写 Git SSH 仓库地址")
    scp = re.fullmatch(r"git@([A-Za-z0-9.-]+):([A-Za-z0-9_./-]+)", value)
    try:
        if scp:
            host, remote = scp.groups()
            port = 22
        else:
            if not re.fullmatch(r"ssh://git@[A-Za-z0-9.-]+(?::[0-9]{1,5})?/[A-Za-z0-9_./-]+", value):
                raise ValueError()
            parsed = urlsplit(value)
            host, remote = parsed.hostname, parsed.path
            port = 22 if parsed.port is None else parsed.port
        from .repository_import import source_url
        _, host, name = source_url("https://" + host + "/" + remote.lstrip("/"))
        parts = remote.removeprefix("/").split("/")
        if not port or any(not part or part in {".", "..", ".git"} for part in parts) or parts[0].startswith("-"):
            raise ValueError()
    except (ValueError, TypeError, W.Problem) as error:
        raise W.Problem("invalid_repository_url", "请输入 git@主机:仓库路径 或 ssh://git@主机/仓库路径") from error
    return value, host, name, remote, port


def openssh(program):
    native = Path(os.environ["SystemRoot"]) / "System32" / "OpenSSH" / (program + ".exe")
    if not native.is_file():
        git = W.git_exe()
        bundled = Path(git).resolve().parent.parent / "usr/bin" / (program + ".exe") if git else None
        if not bundled or not bundled.is_file():
            raise W.Problem("ssh_unavailable", "未找到随附或 Windows OpenSSH 客户端", 503)
        native = bundled
    return native


def _ssh_argv(destination, address):
    native = openssh("ssh")
    selected = getattr(destination, "ssh_identity", None)
    identity = ["-oIdentitiesOnly=yes", "-i", selected["file"]] if selected else []
    alias = destination.host if destination.port == 22 else f"[{destination.host}]:{destination.port}"
    # The local SSH client alone accesses its user's default identities/agent and trusted host keys.
    # No user-configured shell, proxy, forwarding, askpass, or remote command is accepted.
    return [str(native), "-F", "none", "-T", *identity, "-oBatchMode=yes", "-oStrictHostKeyChecking=yes",
            "-oUpdateHostKeys=no", "-oForwardAgent=no", "-oClearAllForwardings=yes",
            "-oPermitLocalCommand=no", "-oProxyCommand=none", "-oPreferredAuthentications=publickey",
            "-oConnectTimeout=12", "-oHostKeyAlias=" + alias, "-p", str(destination.port),
            "--", "git@" + address, "git-upload-pack '" + destination.remote_path + "'"]


def _transport_failure(stderr):
    text = bytes(stderr).lower()
    if any(marker in text for marker in (b"host key verification failed", b"host identification has changed", b"no host key is known", b"host key is known")):
        return W.Problem("ssh_host_untrusted", "请用本机 SSH 客户端核对托管方公布的主机指纹并加入 known_hosts，再重试。", 403)
    if any(marker in text for marker in (b"agent refused operation", b"incorrect passphrase", b"bad permissions", b"error loading key")):
        return W.Problem("ssh_identity_locked", "本机 SSH 身份不可用，请解锁身份或选择其他公钥。", 403)
    if b"permission denied (publickey" in text or b"authentication failed" in text:
        return W.Problem("ssh_access_denied", "SSH 主机没有接受本次身份。请在聊天中选择已有身份，或创建并登记公钥后重试。", 403)
    if any(marker in text for marker in (b"could not resolve hostname", b"connection timed out", b"connection refused", b"network is unreachable", b"no route to host")):
        return W.Problem("import_network", "SSH 未能连接到仓库主机，请核对网络与地址。", 409)
    if any(marker in text for marker in (b"repository not found", b"does not appear to be a git repository",
                                          b"access denied", b"permission denied")):
        return W.Problem("ssh_repository_path", "托管方未提供该仓库；请核对地址及该身份的读取权限。", 409)
    return W.Problem("ssh_repository_unavailable", "SSH 读取失败，请核对仓库路径、访问权限和网络连接。", 409)


def fetch_bundle(destination, bundle, address, check):
    """Request the default branch into a Git bundle without unpacking any remote objects on the host."""
    try:
        from dulwich.client import SSHGitClient, SubprocessWrapper
        from dulwich.config import ConfigDict
        from dulwich.object_store import ObjectStoreGraphWalker
        from dulwich.refs import check_ref_format
    except ImportError as error:
        raise W.Problem("ssh_transport_unavailable", "私库传输组件缺失，请修复 Modot 运行环境。", 503) from error

    check()
    argv = _ssh_argv(destination, address)
    env = {key: os.environ[key] for key in ("SystemRoot", "WINDIR", "USERPROFILE", "HOME", "HOMEDRIVE",
            "HOMEPATH", "LOCALAPPDATA", "APPDATA", "PROGRAMDATA", "TEMP", "TMP", "SSH_AUTH_SOCK") if key in os.environ}
    env["PATH"] = os.pathsep.join((str(Path(argv[0]).parent), str(Path(os.environ["SystemRoot"]) / "System32")))
    proc = subprocess.Popen(argv, cwd=Path(argv[0]).parent, env=env, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        family = Family(proc, processes=4)
    except BaseException:
        proc.kill()
        proc.wait()
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            pipe.close()
        raise
    err_pipe, proc.stderr = proc.stderr, None
    diagnostic = bytearray()
    stopped = threading.Event()
    interruptions = []

    def drain():
        with err_pipe:
            for block in iter(lambda: err_pipe.read(8192), b""):
                diagnostic.extend(block[:max(0, 8192 - len(diagnostic))])

    def watch():
        while not stopped.wait(.15):
            try:
                check()
            except Exception as error:
                interruptions.append(error)
                proc.kill()
                return

    reader = threading.Thread(target=drain, daemon=True)
    watchdog = threading.Thread(target=watch, daemon=True)
    reader.start()
    watchdog.start()
    transport = SubprocessWrapper(proc)

    class ConnectedSSH:
        def run_command(self, *args, **kwargs):
            return transport

    try:
        # An authentication failure closes stdout before any Git data arrives. Handle that
        # before Dulwich creates a protocol, and bound the ref advertisement before parsing.
        raw_read = transport.read
        prefix = bytearray(raw_read(4))
        if len(prefix) != 4 or not re.fullmatch(b"[0-9a-fA-F]{4}", prefix):
            raise ValueError("Missing Git packet header")
        advertisement_bytes = 0
        reading_refs = True

        def bounded_read(size):
            nonlocal advertisement_bytes
            buffered = bytes(prefix[:size])
            del prefix[:len(buffered)]
            block = buffered + raw_read(size - len(buffered))
            if reading_refs:
                advertisement_bytes += len(block)
                if advertisement_bytes > 4 * 1024 * 1024:
                    raise W.Problem("import_capacity", "远端引用清单超过 4 MiB，未建立项目", 413)
            return block

        transport.read = bounded_read
        client = SSHGitClient(destination.host, username="git", vendor=ConnectedSSH(), config=ConfigDict(),
                              thin_packs=False, include_tags=False, quiet=True)
        heads = set()
        with bundle.open("xb") as stream:
            def wants(refs, depth=None):
                nonlocal reading_refs
                reading_refs = False
                head = refs.get(b"HEAD")
                if not head or not re.fullmatch(b"[a-f0-9]{40}|[a-f0-9]{64}", head):
                    raise W.Problem("import_checkout", "远端没有可检出的默认提交", 409)
                heads.update(ref for ref, oid in refs.items() if oid == head and ref.startswith(b"refs/heads/")
                             and len(ref) < 1024 and check_ref_format(ref))
                if not heads or sum(map(len, heads)) > 1048576:
                    raise W.Problem("import_checkout", "远端默认分支引用无效或过大", 409)
                stream.write(b"# v2 git bundle\n" if len(head) == 40 else b"# v3 git bundle\n@object-format=sha256\n")
                for ref in sorted(heads):
                    stream.write(head + b" " + ref + b"\n")
                stream.write(b"\n")
                return [head]

            def receive(block):
                if stream.tell() + len(block) > 512 * 1024 * 1024:
                    raise W.Problem("import_capacity", "私库默认分支传输超过 512 MiB，未建立项目", 413)
                return stream.write(block)

            result = client.fetch_pack(destination.remote_path.encode("ascii"), wants,
                ObjectStoreGraphWalker([], lambda _: []), receive, protocol_version=0)
        if proc.wait() != 0:
            reader.join(timeout=1)
            raise _transport_failure(diagnostic)
        branch = result.symrefs.get(b"HEAD")
        if branch is None and len(heads) == 1:
            branch = next(iter(heads))
        if branch not in heads or branch.removeprefix(b"refs/heads/").startswith(b"-"):
            raise W.Problem("import_checkout", "无法确定远端默认分支，未建立项目", 409)
        check()
        return branch.removeprefix(b"refs/heads/").decode("utf-8"), result.refs[b"HEAD"].decode("ascii")
    except W.Problem:
        if interruptions:
            raise interruptions[0] from None
        raise
    except Exception:
        reader.join(timeout=.25)
        if interruptions:
            raise interruptions[0] from None
        raise _transport_failure(diagnostic) from None
    finally:
        stopped.set()
        family.close()
        proc.wait(timeout=5)
        for pipe in (proc.stdin, proc.stdout):
            if not pipe.closed:
                pipe.close()
        watchdog.join(timeout=2)
        reader.join(timeout=2)
