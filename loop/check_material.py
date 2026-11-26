"""Bounded artifacts exchanged with an isolated project check."""

# 主要引用方包括 `code_service.py` 和 `role_coordination.py`。

import re
import stat

from . import artifacts, evidence, workbench as W

DIRECTORY = ".harness-check"
MAX_BYTES = 256 * 1024


def operation_script(root, thread, resource_id, workspace_id, oid):
    """Resolve only the Python bytes; never inherit command options or authority."""
    from . import code_service as C

    if not isinstance(oid,str) or not re.fullmatch(r'[0-9a-f]{32}',oid):
        raise W.Problem('invalid_arguments','script_operation_id 需要原检查回执的 operationId')

    def owned_check(ref):
        op = C.operation(thread,ref)
        if op['operation'] != 'check':
            raise W.Problem('script_unavailable','该操作没有隔离检查脚本',409)
        rid = op['resourceId']
        bound = workspace_id if rid == resource_id else C.context_resource(root,rid)[2]['id']
        if op.get('preparationOnly') or op.get('workspaceId') != bound:
            raise W.Problem('wrong_source','原检查尚未绑定源码或其资源归属已变更',403)
        return op

    op = owned_check(oid)
    target = op['target']
    origin = target.get('scriptSourceOperationId',oid)
    source = op if origin == oid else owned_check(origin)
    original = source['target']
    if (not target.get('scriptHash') or target['scriptHash'] != original.get('scriptHash')
            or original.get('scenario') or original.get('material')
            or original.get('argv') != ['python','.harness-check/input.py']):
        raise W.Problem('script_unavailable','该操作没有可核对原文的内联 Python 脚本',409)
    script = (source.get('backgroundArgs') or {}).get('script') or source.get('checkScript')
    if script is None and not source.get('backgroundCheck'):
        # Older synchronous runs keep the submitted input in action evidence.
        record = evidence.detail(thread,source['source']['actionId'],C.RuntimeView())
        if record.get('runId') == source['source']['runId'] and record.get('tool') == 'run_project_check':
            script = (record.get('input') or {}).get('script')
    if not isinstance(script,str) or W.digest(script.encode('utf8')) != target['scriptHash']:
        raise W.Problem('script_unavailable','原脚本缺失或内容校验不符，未执行检查',409)
    return script, origin


def prepare(thread, args):
    material = args.get("material")
    output = args.get("retain_output")
    if not ({"script", "material", "retain_output"} & args.keys()):
        return None, None
    if args.get("capture_changes"):
        raise W.Problem("invalid_arguments", "验证材料与产物只用于隔离检查")
    if "script" in args:
        script = args["script"]
        if "material" in args or not isinstance(script, str) or not script.strip() or len(script.encode('utf8')) > MAX_BYTES:
            raise W.Problem("invalid_arguments", "script 须为非空 UTF-8 Python 文本且不超过256 KiB，与material不能同时使用")
        data = script.encode('utf8')
    if "retain_output" in args and (not isinstance(output, str) or not re.fullmatch(
        r"result\.(?:txt|html|json|png|jpg)", output
    )):
        raise W.Problem("invalid_arguments", "请指定 result.txt、result.html、result.json、result.png 或 result.jpg")
    if "script" in args:
        return data, output
    if "material" not in args:
        return None, output
    if not isinstance(material, dict) or set(material) != {"artifact_id", "version_id"} or any(
        not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value) for value in material.values()
    ):
        raise W.Problem("invalid_arguments", "验证材料需要明确的成果和版本")
    item = artifacts.read_version(thread, material["artifact_id"], material["version_id"])
    data = item.get("text", "").encode("utf-8")
    if not data or len(data) > MAX_BYTES:
        raise W.Problem("invalid_arguments", "验证脚本须为非空 UTF-8 文本且不超过 256 KiB")
    return data, output


def stage(target, data):
    folder = target / DIRECTORY
    try:
        folder.mkdir()
    except FileExistsError:
        raise W.Problem("material_path_conflict", "项目已使用验证材料的保留目录") from None
    if data is not None:
        (folder / "input.py").write_bytes(data)


def collect(thread, target, name, source):
    folder = target / DIRECTORY
    file = folder / name
    for path in (target, folder, file):
        info = path.lstat()
        if getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT or path.is_symlink():
            raise ValueError("验证产物不能是链接")
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_BYTES:
        raise ValueError("验证产物须为普通文件且不超过 256 KiB")
    with file.open("rb") as handle:
        data = handle.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("验证产物超过 256 KiB")
    body = {"title": name, "format": "text"}
    if name.endswith(".png") and data.startswith(b"\x89PNG\r\n\x1a\n"):
        image = data, "image/png", {}
    elif name.endswith(".jpg") and data.startswith(b"\xff\xd8\xff"):
        image = data, "image/jpeg", {}
    elif name.endswith((".png", ".jpg")):
        raise ValueError("验证图片格式无效")
    else:
        image = None
        body["text"] = evidence.redact(data.decode("utf-8"))
    saved = artifacts.publish(thread, body, source, prepared_image=image)
    return {"artifactId": saved["id"], "versionId": saved["version"]["id"], "hash": saved["version"]["hash"]}
