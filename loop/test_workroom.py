"""Versioned check materials, not a scheduler. Discovery never executes a recipe."""
# 主要引用方包括 `executor_core.py` 和 `workbench.py`。
import json
from pathlib import Path, PurePosixPath
import re
import stat

from . import workbench as W
from .filelock import FileLock, atomic_json, read_text

LIMIT = 192 * 1024
LIBRARY = Path(__file__).with_name('workroom_kit.py')
GUIDE = dict(import_='from modot_testkit import case; params为本次参数',
    files='case.files({相对路径:文本}); case.read(相对路径); case.path(相对路径)',
    assertions='普通assert，或case.check(condition,label)/case.equal(actual,expected,label)',
    prepared='folder,created=case.prepared(name)：仅保留准备材料，是否重建由你判断；首次environment_id=new',
    command='case.command(argv,cwd=None,timeout=60,expected_exit=0)：不符预期的子进程失败保留，cwd默认当前源码，Python沿用隔离解释器',
    sphinx='out=case.sphinx(files,config=conf_py_text,builder="html"); case.links(out/"index.html")',
    results='打印不代表验收；异常/真实失败与原日志保留；环境和场景材料分开保留')


# `value` 可来自本模块内置配方或用户保存的场景。
# 严格区分 Python 脚本与命令入口，先校验文本材料路径，再形成版本化定义。
def validate(value):
    allowed = {'name', 'description', 'runner', 'script', 'argv', 'files', 'defaults', 'requires'}
    if not isinstance(value, dict) or set(value) - allowed:
        raise W.Problem('scenario_arguments', '场景只接受名称、说明、运行入口、输入文件、默认参数和工具需求')
    value = dict(value)
    if not isinstance(value.get('name'), str) or not 1 <= len(value['name']) <= 100:
        raise W.Problem('scenario_arguments', '场景名称需为1—100字符')
    value.setdefault('description', '')
    if not isinstance(value['description'], str) or len(value['description']) > 2000:
        raise W.Problem('scenario_arguments', '场景说明超过范围')
    runner = value.setdefault('runner', 'python')
    if runner == 'python':
        if not isinstance(value.get('script'), str) or not value['script'].strip() or 'argv' in value:
            raise W.Problem('scenario_arguments', 'Python场景需要script，不能同时传argv')
    elif runner == 'command':
        if ('script' in value or not isinstance(value.get('argv'), list) or not value['argv']
                or not all(isinstance(x, str) and x for x in value['argv'])):
            raise W.Problem('scenario_arguments', '命令场景需要非空argv，不能传script')
    else:
        raise W.Problem('scenario_runner', '当前支持python或command；未启动其他扩展宿主')
    files = value.setdefault('files', {})
    if not isinstance(files, dict) or len(files) > 40:
        raise W.Problem('scenario_arguments', '场景输入最多40个文本文件')
    seen=set()
    for name, content in files.items():
        parts = PurePosixPath(str(name)).parts
        if (not isinstance(name, str) or not parts or '\\' in name or ':' in name
                or PurePosixPath(name).is_absolute() or any(p in {'.', '..'} for p in parts)
                or any(p.endswith(('.', ' ')) for p in parts) or not isinstance(content, str)):
            raise W.Problem('scenario_path', '场景文件须为相对文本路径，不能离开材料目录')
        key='/'.join(parts).casefold()
        if key in seen:raise W.Problem('scenario_path','场景文件路径重复')
        seen.add(key)
    if not isinstance(value.setdefault('defaults', {}), dict):
        raise W.Problem('scenario_arguments', '默认参数须为JSON对象')
    needs = value.setdefault('requires', [])
    if not isinstance(needs, list) or len(needs) > 20 or not all(isinstance(x,str) and len(x)<200 for x in needs):
        raise W.Problem('scenario_arguments', '工具需求须为简短文本列表')
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode('utf8')
    except (TypeError, ValueError):
        raise W.Problem('scenario_arguments', '场景内容须为有效JSON') from None
    if len(raw) > LIMIT:
        raise W.Problem('scenario_capacity', '场景材料超过192KiB')
    return value


def builtins():
    # Framework recipes provide setup/observation, never benchmark assertions.
    return {
        'python': validate(dict(name='Python对象与文件', description='修改输入和断言；from modot_testkit import case。',
            script='from modot_testkit import case\ncase.equal(params.get("value", 4), 4, "示例断言，请按任务修改")')),
        'command': validate(dict(name='已有项目测试', runner='command', argv=['python','-m','unittest','discover'],
            description='使用仓库已有测试入口。修改argv即可，保留真实退出码和原日志。')),
        'django': validate(dict(name='Django既有测试入口', requires=['Django及目标仓库测试依赖'], defaults={'tests':[]},
            description='先读仓库tests/runtests.py与测试设置；params.tests填已有选择器。用原fixture，后端覆盖如实报告。',
            script='from modot_testkit import case\nif not params.get("tests"):\n    case.blocked("请先选择仓库已有测试入口，不默认宽测")\ncase.command(["python", "tests/runtests.py", *params["tests"], "--parallel", "1"])')),
        'sphinx': validate(dict(name='Sphinx最小文档构建', requires=['目标项目Sphinx'], defaults={'builder':'html'},
            description='使用case.sphinx(files, config, builder)构建普通文档，case.links读取链接；自行补断言。',
            files={'docs/index.rst':'Example\n=======\n\nA normal document.\n'},
            script='from modot_testkit import case\ncase.sphinx({"index.rst": case.read("docs/index.rst")}, builder=params.get("builder", "html"))')),
    }


def _root(ident, task):
    # IDs are used as data, never as model-selected paths.
    if not isinstance(ident,str) or not isinstance(task,str) or not ident or not task:
        raise W.Problem('scenario_scope', '场景需要明确任务身份', 403)
    base = W.home() / 'test-workroom'
    root = base / W.digest((ident+'\0'+task).encode())[:24]
    if not root.resolve().is_relative_to(W.home().resolve()):
        raise W.Problem('scenario_path', '场景存储路径变化', 403)
    return root


def _version(value):
    return W.digest(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode())


# `executor_core.py` 的工具入口和 `workbench.py` 的 HTTP 路由传入会话和逻辑任务身份。
# 这里读写场景版本，实际隔离检查由 `code_service.py` 启动。
def manage(ident, task, operation='list', scenario_id=None, version=None, definition=None, expected_version=None):
    root = _root(ident, task)
    if operation == 'list':
        items = [dict(id='builtin:'+key, version=_version(v), name=v['name'], description=v['description'],
                      runner=v['runner'], requires=v['requires'], source='builtin') for key,v in builtins().items()]
        if root.exists():
            for path in sorted(root.glob('*/head.json')):
                row = json.loads(read_text(path))
                items.append({k:row[k] for k in ('id','version','name','description','runner','requires','source')})
        return dict(ok=True, scenarios=items, notice='按需读取所选场景；通过run_project_check(scenario={id,version})运行。')
    if operation == 'read':
        if isinstance(scenario_id,str) and scenario_id.startswith('builtin:'):
            value = builtins().get(scenario_id[8:])
            if value is None:raise W.Problem('scenario_missing','未找到内置场景',404)
            actual = _version(value)
            if version is not None and version != actual:raise W.Problem('scenario_changed','内置场景版本已变化',409)
            return dict(ok=True,id=scenario_id,version=actual,definition=value,source='builtin',helpers=GUIDE)
        if not isinstance(scenario_id,str) or not re.fullmatch('[0-9a-f]{32}',scenario_id):
            raise W.Problem('scenario_missing','场景不属于当前任务',404)
        folder=root/scenario_id
        if version is None:
            if not (folder/'head.json').is_file():raise W.Problem('scenario_missing','场景不属于当前任务',404)
            version=json.loads(read_text(folder/'head.json'))['version']
        if not isinstance(version,str) or not re.fullmatch('[0-9a-f]{64}',version):
            raise W.Problem('scenario_arguments','场景版本无效')
        file=folder/(version[:24]+'.json')
        if not file.is_file():raise W.Problem('scenario_missing','未找到该场景版本',404)
        row=json.loads(read_text(file))
        if _version(row['definition']) != version:raise W.Problem('scenario_changed','保留材料已变化',409)
        return dict(ok=True,**row,helpers=GUIDE)
    if operation != 'save':raise W.Problem('scenario_arguments','操作为list/read/save')
    value=validate(definition)
    import uuid
    ident_scene=scenario_id or uuid.uuid4().hex
    if not re.fullmatch('[0-9a-f]{32}',ident_scene):raise W.Problem('scenario_arguments','内置模板只读；请另存为任务场景')
    root.mkdir(parents=True,exist_ok=True)
    with FileLock(root/'store.lock'):
        folder=root/ident_scene
        if folder.exists():
            prior=json.loads(read_text(folder/'head.json'))
            if expected_version != prior['version']:raise W.Problem('revision_conflict','场景已被修改，请读取后再保存',409)
        elif expected_version is not None:raise W.Problem('revision_conflict','场景不存在，未覆盖',409)
        if len(list(root.glob('*/*.json'))) >= 256:raise W.Problem('scenario_capacity','场景版本保留区已满，请另存需要的材料')
        folder.mkdir(exist_ok=True)
        row=dict(id=ident_scene,version=_version(value),definition=value,source='task')
        path=folder/(row['version'][:24]+'.json')
        if path.exists():
            if json.loads(read_text(path))['version'] != row['version']:raise W.Problem('scenario_changed','版本路径冲突，未覆盖',409)
        else:atomic_json(path,row)
        atomic_json(folder/'head.json',dict({k:v for k,v in row.items() if k!='definition'},
            **{k:value[k] for k in ('name','description','runner','requires')}))
    return dict(ok=True,id=ident_scene,version=row['version'],name=value['name'],source='task')


def resolve(ident, task, reference, parameters=None):
    if not isinstance(reference,dict) or set(reference)!={'id','version'}:
        raise W.Problem('scenario_arguments','场景引用需要id和version')
    if not isinstance(reference['version'],str) or not re.fullmatch('[0-9a-f]{64}',reference['version']):
        raise W.Problem('scenario_arguments','运行需要明确的场景版本')
    row=manage(ident,task,'read',reference['id'],reference['version'])
    if parameters is not None and not isinstance(parameters,dict):raise W.Problem('scenario_arguments','parameters须为JSON对象')
    value=dict(row['definition'],defaults=dict(row['definition']['defaults'],**(parameters or {})))
    validate(value)
    helper=LIBRARY.read_text(encoding='utf8')
    library_version=W.digest(helper.encode())
    payload=json.dumps(value,ensure_ascii=True,allow_nan=False)
    script=helper+'\n\nif __name__ == "__main__":\n    _execute('+repr(payload)+')\n'
    return script,dict(id=row['id'],version=row['version'],libraryVersion=library_version,name=value['name'])


def collect(target):
    path=Path(target)/'.harness-check'/'workroom.json'
    for item in (Path(target),path.parent,path):
        info=item.lstat()
        if item.is_symlink() or getattr(info,'st_file_attributes',0)&0x400:
            raise ValueError('场景结果不能是重定向')
    info=path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size>128*1024:
        raise ValueError('场景结果超过范围')
    result=json.loads(path.read_text(encoding='utf8'))
    if not isinstance(result,dict) or result.get('format')!=1:raise ValueError('场景结果格式无效')
    if (result.get('status') not in {'passed','failed','unasserted','blocked','preparation_failed','error','unknown'}
            or not isinstance(result.get('assertions'),list)
            or not all(isinstance(r,dict) and isinstance(r.get('name'),str) and type(r.get('passed')) is bool
                       and ('selector' not in r or isinstance(r['selector'],str) and len(r['selector'])<=240)
                       for r in result['assertions'])
            or type(result.get('commands')) is not int or result['commands']<0):
        raise ValueError('场景结果字段无效，原日志保留')
    return result
