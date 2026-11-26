"""Approved repository checkout in the current project or the selected work folder."""
# 主要引用方包括 `executor_core.py` 和 `workbench.py`。

from contextlib import closing
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import socket
import stat
import threading
import time
import uuid
from urllib.parse import urlsplit

from . import authority_paths, permissions, projects, repository_staging, work_folders, workbench as W
from .filelock import FileLock


_TIMEOUT = 90
_PRIVATE_STOP = "repository-import-stop.json"
_IMPORTS = {}
_LOCK = threading.Lock()
_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_SYNTHETIC_DNS = ipaddress.ip_network("198.18.0.0/15")


def source_url(value):
    if not isinstance(value, str) or len(value) > 1024 or not re.fullmatch(
        r"https://[a-zA-Z0-9.-]+(?:/[a-zA-Z0-9_.~-]+)+", value
    ):
        raise W.Problem("invalid_repository_url", "请输入公开 HTTPS 仓库地址，不附带凭据、端口或查询参数")
    parsed = urlsplit(value)
    host = parsed.hostname
    if not host or "." not in host or any(
        not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
        for label in host.split(".")
    ) or host.split(".")[-1].isdigit() or any(
        part in {".", "..", ".git"} for part in parsed.path.split("/")
    ):
        raise W.Problem("invalid_repository_url", "请输入公开 HTTPS 仓库的域名和路径")
    return "https://" + host + parsed.path, host, parsed.path.rsplit("/", 1)[-1].removesuffix(".git")


def _public_dns(host):
    # Some local VPN resolvers return synthetic benchmark-range addresses.
    # Resolve the public name over authenticated HTTPS before pinning Git.
    with closing(http.client.HTTPSConnection("cloudflare-dns.com", timeout=6)) as connection:
        connection.request("GET", "/dns-query?name=" + host + "&type=A",
                           headers={"Accept": "application/dns-json"})
        response = connection.getresponse()
        if response.status != 200:
            return set()
        answer = json.loads(response.read(65536))
        if answer.get("Status") != 0:
            return set()
        return {ipaddress.ip_address(row["data"]) for row in answer.get("Answer", []) if row.get("type") == 1}


def public_addresses(host, check):
    # DNS resolution itself can block. A result arriving after cancellation is discarded.
    done = threading.Event()
    addresses = set()

    def resolve():
        try:
            for *_, address in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM):
                addresses.add(ipaddress.ip_address(address[0]))
            if addresses and all(address in _SYNTHETIC_DNS for address in addresses):
                public = _public_dns(host)
                addresses.clear()
                addresses.update(public)
        except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException):
            addresses.clear()
        finally:
            done.set()

    threading.Thread(target=resolve, daemon=True, name="repository-dns").start()
    while not done.wait(.1):
        check()
    check()
    if not addresses:
        raise W.Problem("import_network", "无法解析仓库域名，请检查仓库地址与网络", 502)
    if any(not a.is_global or a.is_multicast or a.is_reserved or a.is_loopback or a.is_link_local for a in addresses):
        raise W.Problem("repository_address_denied", "仓库域名必须只解析到公开网络地址", 403)
    return ",".join(str(a) if a.version == 4 else "[" + str(a) + "]" for a in sorted(addresses, key=str))


def directory_identity(path):
    try:
        info = path.lstat()
    except OSError as error:
        raise W.Problem("import_destination_changed", "工作目录不存在或无法读取", 409) from error
    if (not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & _REPARSE
            or path.resolve(strict=True) != path):
        raise W.Problem("import_destination_changed", "工作目录或链接目标已变化", 409)
    return info.st_dev, info.st_ino


def _project_grant(session_id, project_id):
    scope = projects.scope(session_id)
    primary = scope.get("folderResourceId")
    resource = next((r for r in scope["resources"]
                     if primary and (r.get("worktreeOf") or r["id"]) == primary and r["kind"] == "code"), None)
    captured = scope.get("lastRun") or {}
    if (scope["projectId"] != project_id or captured.get("projectId") != project_id or not resource
            or resource["permission"] != "read" or not resource.get("write")
            or not any(r["id"] == resource["id"] and r.get("path") == resource["path"]
                       and r.get("permission") == "read" and r.get("write") for r in captured.get("resources", []))):
        raise W.Problem("project_import_permission", "请在本会话的项目资料中选中项目目录并允许写入，再发起导入", 403)
    return primary, resource["id"], resource["path"], resource.get("permissionAt")


class Destination:
    def __init__(self, url, name=None, *, private=False, session_id=None, project_id=None):
        if os.name != "nt":
            raise W.Problem("unsupported_platform", "仓库导入需要 Windows AppContainer", 501)
        self.private = private
        if private:
            from .repository_ssh import source
            self.url, self.host, fallback, self.remote_path, self.port = source(url)
        else:
            self.url, self.host, fallback = source_url(url)
        name = fallback if name is None else name
        work_folders.validate_name(name)
        if name.casefold() == ".git" or name.casefold().startswith(repository_staging.PREFIX):
            raise W.Problem("invalid_name", "请选择项目文件夹名称")
        self.session_id = session_id
        self.project_id = project_id
        if project_id:
            self.grant = _project_grant(session_id, project_id)
            _, self.resource_id, path, _ = self.grant
            self.base = self.root = Path(path)
            self.revision = W.digest(json.dumps(self.grant).encode())
        else:
            settings = work_folders.settings()
            self.revision = settings["selectionRevision"]
            if not self.revision:
                raise W.Problem("destination_not_authorized", "请先选择工作总目录", 403)
            self.root = Path(settings["path"])
            self.base = Path(settings["projects"])
        self.target = self.base / name
        self.root_identity = directory_identity(self.root)
        self.check()

    def check(self, base_identity=None):
        if self.project_id:
            changed = _project_grant(self.session_id, self.project_id) != self.grant
        else:
            settings = work_folders.settings()
            changed = settings["selectionRevision"] != self.revision or Path(settings["path"]) != self.root
        if (changed or directory_identity(self.root) != self.root_identity
                or base_identity is not None and directory_identity(self.base) != base_identity):
            raise W.Problem("import_destination_changed", "导入位置或目录授权已变化，请重新核对目的地", 409)
        if self.base.exists() or self.base.is_symlink():
            directory_identity(self.base)
        if (authority_paths.protected(self.target, "write", workspace_root=self.base) or
                any(part.casefold() == ".git" for part in self.base.parts) or
                (not self.project_id and W.local_writer_root(self.base) != self.base) or str(self.root).startswith("\\\\")):
            if self.project_id:
                raise W.Problem("project_import_location", "当前项目目录无法用于导入，请使用可写的本地项目根目录", 403)
            raise W.Problem("destination_not_authorized", "请选择 Modot 与现有 Git 检出目录之外的本地工作总目录", 403)
        if os.path.lexists(self.target):
            raise W.Problem("import_destination_exists", "已有同名文件或文件夹，保留原内容，请选择其他名称", 409)


def _git_runtime(target):
    source = W.git_exe()
    if source:
        installed = Path(source).resolve(strict=True)
        for candidate in (installed.parent.parent, installed.parent.parent.parent):
            for arch in ("ucrt64", "mingw64"):
                runtime = candidate / arch
                binary = runtime / "bin/git.exe"
                helper = runtime / "libexec/git-core/git-remote-https.exe"
                if not helper.is_file():
                    helper = runtime / "bin/git-remote-https.exe"
                ca_bundle = runtime / "etc/ssl/certs/ca-bundle.crt"
                if not all(path.is_file() for path in (binary, helper, ca_bundle)):
                    continue
                if installed not in {binary, candidate / "cmd/git.exe", candidate / "bin/git.exe"}:
                    continue
                (target / "bin").mkdir(parents=True)
                (target / "git-core").mkdir()
                for pattern in ("lib*.dll", "zlib*.dll"):
                    for dll in (runtime / "bin").glob(pattern):
                        shutil.copyfile(dll, target / "bin" / dll.name)
                shutil.copyfile(binary, target / "bin/git.exe")
                shutil.copyfile(helper, target / "git-core/git-remote-https.exe")
                shutil.copyfile(ca_bundle, target / "ca-bundle.crt")
                return
    raise W.Problem("import_git_unavailable", "未找到可隔离运行的 Git for Windows 和 HTTPS 组件", 503)


def _limit_checkout(directory, *, live=False):
    total = count = 0
    pending = [directory]
    while pending:
        folder = pending.pop()
        try:
            entries = os.scandir(folder)
        except FileNotFoundError:
            # Git can remove a queued child during reference migration.
            if live and folder != directory:
                continue
            raise
        with entries:
            for child in entries:
                try:
                    info = child.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if getattr(info, "st_file_attributes", 0) & _REPARSE:
                    raise W.Problem("import_link", "检出包含重解析点，未建立项目", 409)
                count += 1
                if stat.S_ISDIR(info.st_mode):
                    pending.append(child.path)
                else:
                    total += info.st_size
                if count > 50000 or total > 512 * 1024 * 1024:
                    raise W.Problem("import_capacity", "下载与检出超过 512 MiB 或 50000 项，未建立项目", 413)


def _run_git(arguments, cwd, env, profile, checkout, check, *, network=False):
    from . import sandbox_windows

    check()
    try:
        process, family, stdout, stderr = sandbox_windows.launch(
            arguments, cwd, env, profile, internet_client=network, processes=12)
    except OSError as error:
        raise W.Problem("import_isolation", "无法启动隔离的 Git 进程", 503) from error
    output = [bytearray(), bytearray()]

    def read(pipe, saved):
        for block in iter(lambda: pipe.read1(8192), b""):
            saved.extend(block[:max(0, 16384 - len(saved))])

    readers = [threading.Thread(target=read, args=(pipe, buf), daemon=True)
               for pipe, buf in zip((stdout, stderr), output)]
    for reader in readers:
        reader.start()
    try:
        if process.resume() != 1:
            raise W.Problem("import_isolation", "隔离的 Git 进程无法恢复运行", 503)
        while process.poll() is None:
            check()
            _limit_checkout(checkout, live=True)
            time.sleep(.15)
        code = process.returncode
    finally:
        try:
            family.close()
            process.wait(timeout=5)
        finally:
            process._k_close()
            for reader, pipe in zip(readers, (stdout, stderr)):
                reader.join(timeout=5)
                if not reader.is_alive():
                    pipe.close()
    check()
    _limit_checkout(checkout)
    if code:
        # Remote response text is untrusted. Use bounded diagnostics to
        # classify the failure without returning server-controlled output.
        errors = bytes(output[1]).lower()
        if b"unknown option" in errors or b"'refs' is not a git command" in errors:
            raise W.Problem("import_git_version", "当前 Git for Windows 缺少仓库导入所需的引用格式支持", 503)
        if network and any(word in errors for word in (b"not found", b"authentication", b"username", b"403", b"401")):
            raise W.Problem("public_repository_unavailable", "仓库不存在或无法匿名访问", 409)
        if network:
            raise W.Problem("import_network", "HTTPS 下载未完成，请检查网络、证书或仓库地址", 502)
        raise W.Problem("import_checkout", "无法核对检出的提交或文件", 409)
    return bytes(output[0])


def _download(destination, stage, profile, check):
    from .safe_executor import _acl
    from .contain import sid_str

    checkout, runtime, scratch = (stage / leaf for leaf in ("checkout", "git", "scratch"))
    checkout.mkdir()
    scratch.mkdir()
    (scratch / "hooks").mkdir()
    _git_runtime(runtime)
    sid = sid_str(profile["sid"])
    if not sid:
        raise W.Problem("import_isolation", "无法分配隔离目录权限", 503)
    _acl(stage, sid, "RX", recursive=False)
    _acl(checkout, sid, "M", recursive=False)
    _acl(scratch, sid, "M", recursive=False)
    windows = Path(os.environ.get("SystemRoot", "C:/Windows"))
    # The Git child sees only a fresh profile and trusted runtime. Pin Git's
    # requests to addresses already checked by the host, with normal TLS checks.
    env = dict(SystemRoot=str(windows), WINDIR=str(windows), PATH=os.pathsep.join(
        (str(runtime / "bin"), str(windows / "System32"))), TEMP=str(scratch), TMP=str(scratch),
        HOME=str(scratch), USERPROFILE=str(scratch), XDG_CONFIG_HOME=str(scratch),
        TMPDIR=str(scratch), APPDATA=str(scratch), LOCALAPPDATA=str(scratch),
        GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0",
        GIT_EXEC_PATH=str(runtime / "git-core"), GIT_PROTOCOL_FROM_USER="0",
        GIT_CEILING_DIRECTORIES=str(stage), GIT_OPTIONAL_LOCKS="0")
    arguments = [str(runtime / "bin/git.exe"), "-c", "core.hooksPath=" + str(scratch / "hooks"),
                 "-c", "core.fsmonitor=false", "-c", "core.symlinks=false", "-c", "core.longpaths=true",
                 "-c", "core.protectNTFS=true", "-c", "credential.helper=", "-c", "credential.interactive=false",
                 "-c", "protocol.allow=never", "-c", "transfer.fsckObjects=true"]
    clone = ["clone", "--single-branch", "--no-tags", "--no-recurse-submodules", "--template=", "--ref-format=reftable"]
    addresses = public_addresses(destination.host, check)
    expected_head = None
    if destination.private:
        from .repository_ssh import fetch_bundle
        bundle = stage / "received.bundle"
        ref, expected_head = fetch_bundle(destination, bundle, addresses.split(",")[0].strip("[]"), check)
        _acl(bundle, sid, "R", recursive=False)
        _run_git(arguments + ["-c", "protocol.file.allow=always"] + clone + ["--branch", ref, "--", str(bundle), "."],
                 checkout, env, profile, checkout, check)
        _run_git(arguments + ["remote", "set-url", "origin", destination.url], checkout, env, profile, checkout, check)
    else:
        https = ["-c", "protocol.https.allow=always",
                 "-c", "http.followRedirects=false", "-c", "http.proxy=", "-c", "http.sslVerify=true",
                 "-c", "http.emptyAuth=false", "-c", "http.sslCAInfo=" + str(runtime / "ca-bundle.crt"),
                 "-c", "http.curloptResolve=" + destination.host + ":443:" + addresses]
        _run_git(arguments + https + clone + ["--depth=1", "--", destination.url, "."],
                 checkout, env, profile, checkout, check, network=True)
    _run_git(arguments + ["refs", "migrate", "--ref-format=files"], checkout, env, profile, checkout, check)
    head = _run_git(arguments + ["rev-parse", "--verify", "HEAD"], checkout, env, profile, checkout, check).decode("ascii").strip()
    branch = _run_git(arguments + ["symbolic-ref", "--short", "HEAD"], checkout, env, profile, checkout, check).decode("utf-8").strip()
    if (not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head) or (expected_head and head != expected_head)
            or not branch or (checkout / ".git/HEAD").read_text(encoding="utf-8").strip() != "ref: refs/heads/" + branch
            or _run_git(arguments + ["status", "--porcelain=v1", "--untracked-files=normal"], checkout, env,
                        profile, checkout, check)):
        raise W.Problem("import_checkout", "远端没有可核对的默认分支和干净检出，未建立项目", 409)
    return checkout, head, branch


def import_project(destination, stopped, permission_revision):
    from . import contain, netguard
    from .safe_executor import _delete_profile

    deadline = time.monotonic() + _TIMEOUT
    def check():
        if stopped():
            raise W.Problem("cancelled", "已取消仓库导入", 409)
        if permissions.profile(destination.session_id)["revision"] != permission_revision or netguard.offline_on():
            raise W.Problem("approval_revoked", "权限或网络设置已变化，仓库导入已停止", 403)
        if time.monotonic() >= deadline:
            raise W.Problem("import_timeout", "仓库导入超时，未建立项目", 504)
        destination.check()

    try:
        check()
        # The staging directory and final checkout are on the same volume.
        lock = W.home() / "registry.lock" if destination.project_id else work_folders.selection_lock()
        with FileLock(lock, check=check):
            destination.check()
            if not destination.project_id:
                destination.base.mkdir(exist_ok=True)
            base_identity = directory_identity(destination.base)
            staging = repository_staging.create(destination.base, project_id=destination.project_id)
        stage = staging.path
        profile_name = "modot.import." + uuid.uuid4().hex
        published = False
        result = None
        try:
            profile = contain.ensure_profile(profile_name)
            if not profile["ok"]:
                raise W.Problem("import_isolation", "无法建立仓库专用 AppContainer", 503)
            checkout, head, branch = _download(destination, stage, profile, check)

            def publish(target):
                nonlocal published
                check()
                destination.check(base_identity)
                if target != destination.target or directory_identity(stage) != staging.stage_key[:2]:
                    raise W.Problem("import_destination_changed", "核对后导入目录身份已变化", 409)
                directory_identity(checkout)
                try:
                    # On Windows rename fails if a same-name path was created meanwhile.
                    checkout.rename(target)
                except FileExistsError as error:
                    raise W.Problem("import_destination_exists", "已有同名文件夹，未覆盖", 409) from error
                published = True

            # 项目内导入保存子目录定位，项目外导入登记新项目。两处共用同一检出发布路径。
            if destination.project_id:
                def publish_in_project(data):
                    publish(destination.target)
                    data["threads"][destination.session_id]["workroomLocation"] = dict(
                        projectId=destination.project_id, resourceId=destination.resource_id, directory=destination.target.name)
                W.change_registry(publish_in_project)
                ownership = dict(projectId=destination.project_id, resourceId=destination.resource_id,
                                 relativePath=destination.target.name)
            else:
                with FileLock(lock, check=check):
                    project = projects.open_folder(str(destination.target), True, publish=publish,
                                                   importing_session=destination.session_id)
                    ownership = dict(project=project, projectId=project["id"], resourceId=project["folderResourceId"])
            result = dict(ok=True, **ownership,
                        destination=str(destination.target), url=destination.url, head=head, branch=branch,
                        shallow=not destination.private,
                        isolation="ssh-transport-appcontainer-checkout" if destination.private else "appcontainer-public-https",
                        notice=("已导入当前项目，可在本会话按 relativePath 读取仓库。" if destination.project_id
                                else "已登记为项目。可在工作间查看代码。"))
        except (OSError, ValueError, RuntimeError) as error:
            if published:
                raise W.Problem("import_registration_failed",
                    f"检出已下载到 {destination.target}，登记失败，可用「打开已有文件夹」重新关联", 503) from error
            raise
        finally:
            _delete_profile(profile_name)
            if not staging.cleanup() and result:
                result["notice"] += " 临时目录仍待清理。"
        return result
    except W.Problem:
        raise
    except (OSError, ValueError, RuntimeError) as error:
        raise W.Problem("import_unavailable", "导入目录或隔离运行时不可用，请检查工作目录和 Git", 503) from error


def tool(url, name=None):
    return _session_import(url, name, private=False)


def private_tool(url, name=None):
    return _session_import(url, name, private=True)


def cancel_private_import(directory, run_id):
    """Record an explicit Stop separately from step-boundary interjections."""
    if run_id:
        W.atomic_json(Path(directory) / _PRIVATE_STOP, {"runId": run_id})


def _session_import(url, name, *, private):
    from .tools import current_call, current_stop

    call = current_call() or {}
    ident = call.get("sessionId")
    stop_tool = current_stop() or (lambda: False)
    def validate():
        if not ident or not call.get("nativeExecutor") or call.get("role") != "executor":
            raise W.Problem("import_scope", "仓库导入只接受用户会话中的原生执行者申请", 403)
        if permissions.effective_sandbox() == "read-only":
            raise W.Problem("sandbox_read_only", "当前会话只读，不能导入仓库", 403)
        thread = projects.conversation(ident)
        if any(thread.get(field) for field in ("readonlyEvidence", "subtaskOf", "archived", "scheduledRun")):
            raise W.Problem("import_scope", "仓库导入只接受用户会话中的原生执行者申请", 403)
        return thread.get("projectId")

    try:
        project_id = validate()
        destination = Destination(url, name, private=private, session_id=ident, project_id=project_id)
        if private:
            from . import ssh_identities
            destination.ssh_identity = ssh_identities.identity(destination)
        revision = permissions.profile(ident)["revision"]
        run_id = call.get("runId")
        stop_file = W.P.data_root() / "runs" / ident / _PRIVATE_STOP if private and run_id else None

        def stopped():
            if stop_tool():
                return True
            if stop_file is None:
                return False
            requested = W.read(stop_file)
            return isinstance(requested, dict) and requested.get("runId") == run_id

        def validate_destination():
            if validate() != project_id:
                raise W.Problem("import_destination_changed", "会话项目已变化，请重新发起导入", 409)
            if private and stopped():
                raise W.Problem("cancelled", "已取消仓库导入", 409)
            destination.check()
            if permissions.profile(ident)["revision"] != revision:
                raise W.Problem("approval_revoked", "权限配置已改变，请重新核对", 403)

        label = "使用本机 SSH 身份导入私库" if private else "下载公开仓库"
        binding = (dict(projectId=project_id, resourceId=destination.resource_id, projectFolderRevision=destination.revision)
                   if project_id else dict(workFolderRevision=destination.revision))
        permissions.authorize(dict(kind="repository-import", label=label,
            url=destination.url, destination=str(destination.target), **binding,
            **({"sshFingerprint": destination.ssh_identity["fingerprint"]} if private and destination.ssh_identity else {})),
            human_only=True, validate_live=validate_destination)
        if private and ssh_identities.identity(destination) != destination.ssh_identity:
            raise W.Problem("ssh_identity_unavailable", "批准期间所选 SSH 身份已变化，请重新确认", 409)
        return import_project(destination, stopped, revision)
    except W.Problem as error:
        return dict(ok=False, code=error.code, error=error.message)


def _receipt_path(ident):
    if not re.fullmatch(r"[a-f0-9]{32}", ident):
        raise W.Problem("invalid_import", "仓库导入编号无效")
    return W.home() / "repository-imports" / (ident + ".json")


def stop_owned():
    with _LOCK:
        for stop in _IMPORTS.values():
            stop.set()


def route(method, parts, body):
    if not parts and method == "POST":
        if (body.get("confirm") is not True or "workFolderRevision" not in body
                or set(body) - {"url", "name", "confirm", "workFolderRevision", "_request_id"}):
            raise W.Problem("confirmation", "请核对仓库地址和本地目的地后确认导入", 403)
        destination = Destination(body.get("url"), body.get("name"))
        if body["workFolderRevision"] != destination.revision:
            raise W.Problem("import_destination_changed", "工作总目录选择已变化，请刷新后确认", 409)
        with _LOCK:
            if len(_IMPORTS) >= 2:
                raise W.Problem("import_busy", "已有仓库正在导入，请等候完成", 409)
            ident = uuid.uuid4().hex
            receipt = dict(id=ident, state="running", url=destination.url, destination=str(destination.target))
            path = _receipt_path(ident)
            revision = permissions.profile()["revision"]
            W.atomic_json(path, receipt)
            stop = threading.Event()
            _IMPORTS[ident] = stop

            def run():
                try:
                    result = import_project(destination, stop.is_set, revision)
                    receipt.update(result, state="completed")
                except W.Problem as error:
                    receipt.update(code=error.code, error=error.message,
                                   state="cancelled" if error.code == "cancelled" else "failed")
                finally:
                    try:
                        W.atomic_json(path, receipt)
                    finally:
                        with _LOCK:
                            _IMPORTS.pop(ident, None)

            worker = threading.Thread(target=run, daemon=True, name="repository-import")
            worker.start()
            return 202, dict(id=ident, state="running", url=destination.url, destination=str(destination.target))
    if parts:
        ident = parts[0]
        with _LOCK:
            row = W.read(_receipt_path(ident))
            stop = _IMPORTS.get(ident)
        if not row:
            raise W.Problem("not_found", "导入回执不存在", 404)
        if row["state"] == "running" and stop is None:
            row.update(state="interrupted", code="import_interrupted",
                       error="导入进程已中断。请核对原目录或项目列表。")
        if len(parts) == 1 and method == "GET":
            return 200, row
        if parts[1:] == ["cancel"] and method == "POST":
            if row["state"] == "running" and stop:
                stop.set()
                row["state"] = "stopping"
            return 200, row
    raise W.Problem("not_found", "仓库导入入口不存在", 404)
