"""Launch a single approved, hash-pinned VSIX on demand for an application capability."""
# 主要引用方包括 `app_formatting.py` 和 `app_testing.py`。

# 供 `app_testing.py` 和 `app_formatting.py` 复用原生扩展宿主。运行参数由槽位适配器给出，应用宿主在一次调用内启动和回收。
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time

from . import workbench as W
from .filelock import atomic_json
from .process_family import Family


# 槽位提供参数名和说明，这里生成 `modot.app-tools/1` 的工具声明，参数为必填字符串且不接受额外字段。
def tool(name, description, arguments, readonly):
    return dict(
        name=name,
        description=description,
        inputSchema=dict(
            type="object",
            additionalProperties=False,
            properties={key: dict(type="string", description=desc) for key, desc in arguments.items()},
            required=list(arguments),
        ),
        annotations=dict(readOnlyHint=readonly, destructiveHint=False),
    )


# 用 `projects.py` 查会话登记，用 `code_service.py` 核对资源及指定能力，返回当前授权检出的实际路径。
def checkout(ident, resource_id, capability=None):
    from . import code_service, projects

    thread = projects.conversation(ident)
    root = W.workspace(thread["workspaceId"])["path"]
    _, resource, _, _ = code_service.context_resource(root, resource_id, capability)
    return Path(resource["path"])


# `ident`、`package` 和 `validate_live` 分别是会话、安装包和实时核验回调，供测试与格式化适配器共用。
class Client:
    """Agent-side adapter lifecycle; discovery alone launches no native process."""

    protocol = "modot.app-tools/1"

    # `app_hosted_extensions.py` 根据当前应用、槽位和插件版本创建此适配器。
    def __init__(self, ident, package, validate_live):
        self.ident, self.package, self.live = ident, package, validate_live

    # Agent 进入适配器时先验证授权。原生宿主在 `run` 收到具体调用后启动。
    def start(self):
        self.live()

    # 单次宿主的进程清理由 `run` 的收尾处理。
    def close(self):
        pass


# 优先使用部署的运行时，再查看仓库内宿主。`marker` 由槽位指定，表示它依赖的宿主能力。
def runtime(marker=None):
    """Only host configuration selects executables. Neither tool arguments nor packages can override them."""
    home = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local/share") / "Modot/hosts"
    desktop = Path(__file__).resolve().parents[1] / "desktop"
    source = desktop / "app-hosts/theia-testing"
    candidates = [home / "theia-editor-v1", home / "theia-testing-v1", source]
    root = Path(
        os.environ.get("MODOT_THEIA_TEST_HOST")
        or next(
            (
                p
                for p in candidates
                if (p / "lib/backend/main.js").is_file() and (not marker or (p / marker).is_file())
            ),
            source,
        )
    )
    executable = "electron.exe" if os.name == "nt" else "electron"
    electron = os.environ.get("MODOT_TEST_ELECTRON") or next(
        (
            str(p)
            for p in (root / "electron" / executable, desktop / "node_modules/electron/dist" / executable)
            if p.is_file()
        ),
        None,
    )
    node = os.environ.get("MODOT_TEST_NODE") or shutil.which("node")
    driver = root / "driver.cjs"
    if not driver.is_file():
        driver = source / "driver.cjs"
    # 可用性要求 Electron、Node、驱动和 Theia 后端均已定位到实际文件，再核对槽位标记。
    if not all(
        p and Path(p).is_absolute() and Path(p).is_file()
        for p in (electron, node, driver, root / "lib/backend/main.js")
    ):
        return None
    if marker and not (root / marker).is_file():
        return None
    return root, driver, electron, node


# 调用者交付固定包指纹、文件、权限检查回调与请求正文。测试使用磁盘文件，格式化另带 `draft`。
def run(
    job,
    package,
    root,
    file,
    check,
    *,
    inspect,
    maximum,
    prefix,
    request,
    draft=None,
    result_limit=256 * 1024,
    marker=None,
):
    settings = runtime(marker)
    if not settings:
        raise W.Problem("app_host_unavailable", "请先准备可选的 Theia 扩展宿主。", 409)
    runtime_dir, driver, electron, node = settings
    check()
    original = Path(package["root"]) / "original.vsix"
    if original.is_symlink() or original.resolve() != original or original.stat().st_size > maximum:
        raise W.Problem("plugin_changed", "保留的扩展原包已变化。", 409)
    raw = original.read_bytes()
    if W.digest(raw) != job["sha256"]:
        raise W.Problem("plugin_changed", "保留的扩展原包与批准的指纹不匹配。", 409)
    # 每次调用单独解包到临时目录，使用槽位的校验与解包函数，并限制子进程继承的环境。
    with tempfile.TemporaryDirectory(prefix=prefix, ignore_cleanup_errors=True) as temp:
        work = Path(temp)
        installed = work / "plugins" / "child"
        inspect(raw, installed)
        env = {
            k: v
            for k, v in os.environ.items()
            if k.upper()
            in {
                "SYSTEMROOT",
                "SYSTEMDRIVE",
                "WINDIR",
                "PATH",
                "PATHEXT",
                "COMSPEC",
                "TEMP",
                "TMP",
                "HOME",
                "USERPROFILE",
                "HOMEDRIVE",
                "HOMEPATH",
                "APPDATA",
                "LOCALAPPDATA",
                "DISPLAY",
                "WAYLAND_DISPLAY",
                "XDG_RUNTIME_DIR",
                "LANG",
            }
        }
        env["PATH"] = str(Path(node).parent) + os.pathsep + env.get("PATH", "")
        # 仅在格式化等明确需要时把草稿交给宿主临时文件，目标项目中的源文件由工作间保存入口管理。
        if draft is not None:
            (work / "draft.utf8").write_bytes(draft.encode("utf8"))
        check()
        process = subprocess.Popen(
            [electron, str(driver)],
            cwd=work,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=os.name != "nt",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        family = None
        try:
            family = Family(process)
            atomic_json(
                work / "request.json",
                dict(
                    runtime=str(runtime_dir),
                    node=node,
                    plugins=str(installed.parent),
                    workspace=str(root),
                    **request,
                    file=str(file),
                    runId=job["runId"],
                    hostPid=process.pid,
                    extension=job["extensionId"] + "@" + job["version"],
                ),
            )
            # 宿主执行期间间隔核对权限和调用状态，超时或核验失效时进入统一清理。
            end = time.monotonic() + 200
            while process.poll() is None:
                if time.monotonic() >= end:
                    raise W.Problem("app_host_timeout", "扩展宿主未在期限内完成。", 504)
                try:
                    process.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    check()
            check()
            output = work / "result.json"
            if process.returncode or not output.is_file() or output.stat().st_size > result_limit:
                raise W.Problem("app_host_failed", "扩展宿主退出且未提交可读取的结果。", 409)
            receipt = json.loads(output.read_text("utf8"))
            # 版本号和 `runId` 必须同时匹配，才读取当前调用的结构化结果。
            if receipt.get("protocol") != 1 or receipt.get("runId") != job["runId"]:
                raise W.Problem("app_host_receipt", "扩展回执不属于此次调用。", 409)
            if not isinstance(receipt.get("result"), dict):
                raise W.Problem(
                    "app_no_result", str(receipt.get("error") or "扩展没有报告结果。")[:1400], 409
                )
            return receipt["result"]
        # 失败、超时、撤权和正常退出都回收进程族，以免下次调用复用旧扩展宿主。
        finally:
            if os.name != "nt":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if family is not None:
                family.close()
            if process.poll() is None:
                process.kill()
            process.wait(timeout=8)
