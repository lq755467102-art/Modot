"""User work files live beside Modot data, never inside conversation metadata."""
# 主要引用方包括 `workbench.py` 和 `projects.py`。
import json
import os
import re
import time
import uuid
from pathlib import Path
from . import workbench as W
from .filelock import FileLock


def settings():
    default = (W.P.data_root() / 'workspaces').resolve()
    app = W.P.app_root().resolve()
    if default.is_relative_to(app):
        # Source launches may store Modot metadata beside its code. A new project
        # there would inherit the host checkout and its runtime write protection.
        local = Path(os.environ.get('LOCALAPPDATA') or os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share')
        profile = Path(os.environ.get('HARNESS_USER_DATA') or local / 'Modot').resolve()
        default = ((local / 'Modot' if profile.is_relative_to(app) else profile) / 'workspaces').resolve()
    file = W.home() / 'work-folders.json'
    root, revision = default, None
    if file.exists():
        try:
            row = json.loads(file.read_text(encoding='utf8'))
            if not isinstance(row, dict) or not isinstance(row.get('path'), str) or not Path(row['path']).is_absolute():
                raise ValueError('path')
            root = Path(row['path']).resolve()
            revision = row.get('revision') or W.digest(str(root).encode('utf-8'))
        except (ValueError, OSError, TypeError, RuntimeError) as error:
            raise W.Problem('work_folder_settings', '工作目录设置无法读取，请在常规设置中重新选择', 409) from error
    return dict(path=str(root), defaultPath=str(default), projects=str(root / 'projects'),
                conversations=str(root / 'conversations'), selectionRevision=revision)


def configure(raw):
    if not isinstance(raw, str) or not Path(raw).is_absolute():
        raise W.Problem('invalid_path', '请选择工作总目录')
    try:
        root = Path(raw).resolve(strict=True)
        if not root.is_dir(): raise ValueError('directory')
    except (OSError, ValueError, RuntimeError) as error:
        raise W.Problem('invalid_path', '工作总目录不存在或无法访问') from error
    if root.is_relative_to(W.P.app_root().resolve()):
        raise W.Problem('protected_runtime', '请选择 Modot 程序目录之外的工作总目录，已有项目仍保留原位置', 409)
    with FileLock(selection_lock()):
        W.atomic_json(W.home() / 'work-folders.json', dict(path=str(root), revision=uuid.uuid4().hex))
    return settings()


def selection_lock():
    return W.home() / 'work-folders.lock'


def validate_name(name):
    if (not isinstance(name, str) or not name.strip() or len(name) > 80
            or re.search(r'[<>:"/\\|?*\x00-\x1f]', name) or name != name.strip()
            or name.endswith('.') or name in {'.', '..'}
            or re.fullmatch(r'(?i)(con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\..*)?', name)):
        raise W.Problem('invalid_name', '请输入有效的文件夹名称，不要包含路径或特殊符号')


def create(kind, name):
    if kind not in {'projects', 'conversations'}:
        raise W.Problem('invalid_path', '未知工作目录类别')
    validate_name(name)
    root = Path(settings()['path'])
    if root.resolve().is_relative_to(W.P.app_root().resolve()):
        raise W.Problem('protected_runtime', '工作总目录位于 Modot 程序目录内，请在常规设置中更改新项目的位置', 409)
    base = root / kind
    try:
        base.mkdir(parents=True, exist_ok=True)
        if base.resolve() != base or not base.is_dir():
            raise W.Problem('work_folder_moved', '工作分类目录的链接目标已变化，请检查设置', 409)
        for index in range(1, 1001):
            target = base / (name if index == 1 else f'{name} ({index})')
            try:
                target.mkdir()
                return target
            except FileExistsError:
                continue
    except OSError as error:
        raise W.Problem('work_folder_unavailable', '无法创建工作文件夹，请在常规设置中检查目录权限', 409) from error
    raise W.Problem('work_folder_collision', '同名文件夹过多，请换一个名称', 409)


def conversation_resource():
    target = create('conversations', time.strftime('会话-%Y%m%d-%H%M%S'))
    return dict(id=uuid.uuid4().hex, name=target.name, path=str(target), kind='code',
                permission='read', permissionAt=time.time(), permissionSource='user', write=True, execute=True)


def ensure_conversation(ident):
    from . import projects
    from .code_service import RuntimeView
    if not isinstance(ident, str) or not ident:
        raise W.Problem('work_folder_scope', '请选择一个独立会话')
    thread = projects.conversation(ident)
    if thread.get('projectId') or thread.get('readonlyEvidence') or thread.get('subtaskOf') or thread.get('archived'):
        raise W.Problem('work_folder_scope', '此会话请使用原项目或审查目录', 409)
    directory = RuntimeView()._resolve_sid(ident)
    if directory and W.RT.status(directory)['active']:
        raise W.Problem('active_run', '请等待本轮结束后创建工作文件夹', 409)
    def save(data):
        current = data['threads'][ident]
        if current.get('projectId') or current.get('readonlyEvidence') or current.get('subtaskOf') or current.get('archived'):
            raise W.Problem('work_folder_scope', '会话归属已变化，请刷新', 409)
        if not current.get('workResource'):
            current['workResource'] = conversation_resource()
        current['resourceIds'] = list(dict.fromkeys([*current.get('resourceIds', []), current['workResource']['id']]))
        return current['workResource']
    resource = W.change_registry(save)
    if directory is not None:
        from .conversation_record import schedule
        schedule(directory)
    return resource
