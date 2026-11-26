"""Agent-side testing slot. Discovery is metadata-only. Each approved run owns a disposable host."""
# 主要引用方包括 `app_hosted_extensions.py` 和 `extension_host.py`。

import contextvars
import json
import re
import threading
import uuid

from . import workbench as W
from . import extension_host
from .extension_host import checkout, runtime, tool
from .testing_vsix import MAX_VSIX, TESTING_CATEGORY as CATEGORY, archive_info as inspect_archive

SLOT = "vscode.testing/1"
ARCHIVE_FILE = "original.vsix"
MAX_ARCHIVE = MAX_VSIX


TOOLS = [
    tool(
        "run_tests",
        "通过此子插件的测试控制器运行一个已保存的项目测试文件。返回 runId，使用 read_test_run 读取断言。每次执行均需人工批准。",
        dict(resource_id="当前会话有执行权限的代码资源 ID", test_file="相对于该检出目录的测试文件"),
        False,
    ),
    tool(
        "read_test_run",
        "读取此会话已有 runId 的测试结果。running 表示仍在执行，可以用同一 ID 再读。包含扩展报告的用例、状态、失败消息和位置。",
        dict(run_id="run_tests 返回的 runId"),
        True,
    ),
]
_lock = threading.Lock()
_jobs = {}


def selected_file(ident, args):
    """Resolve the Agent's relative selection against both the current and captured project execute grant."""
    if set(args) != {"resource_id", "test_file"} or not isinstance(args["resource_id"], str):
        raise W.Problem("app_arguments", "需要代码资源及其相对测试文件。")
    root = checkout(ident, args["resource_id"], "execute")
    relative = args["test_file"]
    if (
        not isinstance(relative, str)
        or not 0 < len(relative) <= 400
        or re.search(r"[\\:\x00-\x1f]", relative)
        or any(p in ("", ".", "..", ".git") for p in relative.split("/"))
    ):
        raise W.Problem("app_arguments", "请选择检出目录内的相对测试文件。")
    file = root / relative
    if not file.is_file() or file.resolve() != file or not file.resolve().is_relative_to(root):
        raise W.Problem("app_file", "测试文件不存在或位置已变化。", 409)
    return root, file


def view(job):
    return {
        k: v
        for k, v in job.items()
        if k
        in (
            "runId",
            "parentId",
            "slot",
            "extensionId",
            "version",
            "sha256",
            "resourceId",
            "testFile",
            "state",
            "result",
            "error",
            "code",
        )
    }


# 临时测试宿主沿用发起会话的检出目录。启动和运行期间都复核同一资源及范围版本。
def run_host(job, package, live):
    from . import projects

    args = dict(resource_id=job["resourceId"], test_file=job["testFile"])
    root, file = selected_file(job["owner"], args)
    scope_revision = projects.scope(job["owner"])["revision"]

    def check():
        live()
        if (
            root != job["root"]
            or projects.scope(job["owner"])["revision"] != scope_revision
            or root.resolve() != root
        ):
            raise W.Problem("app_scope_changed", "运行期间检出目录已变化。", 409)

    result = extension_host.run(
        job,
        package,
        root,
        file,
        check,
        inspect=inspect_archive,
        maximum=MAX_ARCHIVE,
        prefix="modot-app-test-",
        request={},
    )
    if not result.get("totals"):
        raise W.Problem("app_no_result", "扩展没有报告用例结果。", 409)
    return result


def start(ident, package, args, live):
    """Correlate retries with the original Agent action and pin a pollable run to one checkout and grant."""
    from .tools import current_call

    root, _ = selected_file(ident, args)
    if not runtime():
        raise W.Problem("app_host_unavailable", "请先准备可选的 Theia 测试宿主。", 409)
    action = current_call()
    key = (ident, action["runId"], action["actionId"], package["id"])
    with _lock:
        for existing in _jobs.values():
            if existing["key"] == key:
                if (existing["resourceId"], existing["testFile"]) != (args["resource_id"], args["test_file"]):
                    raise W.Problem("app_conflict", "此动作已用于另一个测试文件。", 409)
                return view(existing)
        active = [j for j in _jobs.values() if not j["done"].is_set()]
        if len(active) >= 2 or any(j["root"] == root for j in active):
            raise W.Problem("app_host_busy", "所选检出目录或宿主仍有测试在运行。", 409)
        if len(_jobs) >= 96:
            _jobs.pop(next(k for k, v in _jobs.items() if v["done"].is_set()))
        job = dict(
            package["applicationBinding"],
            runId=uuid.uuid4().hex,
            key=key,
            owner=ident,
            packageId=package["id"],
            revision=package["revision"],
            agentRun=action["runId"],
            resourceId=args["resource_id"],
            testFile=args["test_file"],
            root=root,
            state="running",
            done=threading.Event(),
        )
        _jobs[job["runId"]] = job

    def finish():
        try:
            result = dict(state="completed", result=run_host(job, package, live))
        except Exception as error:
            result = dict(
                state="failed",
                code=getattr(error, "code", "app_host_failed"),
                error=getattr(error, "message", str(error))[:1400],
            )
        with _lock:
            job.update(result)
            job["done"].set()

    context = contextvars.copy_context()
    threading.Thread(
        target=context.run, args=(finish,), daemon=True, name="app-test-" + job["runId"][:8]
    ).start()
    with _lock:
        return view(job)


class Client(extension_host.Client):
    """Use the same discovery, approval and model-receipt path as MCP, without a public control server."""

    def tools(self):
        return TOOLS

    def destination(self, name, args):
        binding = self.package["applicationBinding"]
        label = binding["parentId"] + " / " + binding["extensionId"]
        if name == "run_tests":
            root, _ = selected_file(self.ident, args)
            return label + " · 本机原生扩展和项目测试将以当前系统用户身份运行 · " + str(root)
        return label

    def request(self, method, params):
        from .tools import current_call

        if method != "tools/call" or not self.ident:
            raise W.Problem("app_agent_required", "需要有效的 Agent 会话调用。", 403)
        self.live()
        args = params["arguments"]
        if params["name"] == "run_tests":
            data = start(self.ident, self.package, args, self.live)
        elif params["name"] == "read_test_run":
            if set(args) != {"run_id"} or not isinstance(args["run_id"], str):
                raise W.Problem("app_arguments", "请输入原调用返回的 runId。")
            with _lock:
                job = _jobs.get(args["run_id"])
            if not job or (job["owner"], job["packageId"], job["revision"], job["agentRun"]) != (
                self.ident,
                self.package["id"],
                self.package["revision"],
                current_call()["runId"],
            ):
                raise W.Problem("app_run_denied", "本轮没有此子插件测试回执。", 403)
            job["done"].wait(3)
            self.live()
            checkout(self.ident, job["resourceId"])
            with _lock:
                data = view(job)
        else:
            raise W.Problem("app_tool", "未登记此工具。", 404)
        return dict(
            isError=data["state"] == "failed",
            content=[dict(type="text", text=json.dumps(data, ensure_ascii=False))],
        )
