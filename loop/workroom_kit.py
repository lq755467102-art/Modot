"""Small standard-library helpers, staged inside the existing isolated check."""
# 主要引用或加载入口包括 `test_workroom.py`。
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import traceback
import ast
from html.parser import HTMLParser


class EnvironmentBlocked(Exception):
    pass


# 隔离场景的运行内对象，由本模块 `_execute` 创建。
# `root` 指向本次材料目录，`results` 保存实际断言，`commands` 计数子命令，`phase` 区分准备、执行与断言。
class Case:
    def __init__(self, root):
        self.root=Path(root).resolve()
        self.results=[]
        self.commands=0
        self.phase='prepare'

    def path(self, name, root=None):
        base=Path(root or self.root).resolve()
        relative=PurePosixPath(str(name))
        if relative.is_absolute() or '..' in relative.parts or '\\' in str(name) or ':' in str(name):
            raise ValueError('测试材料须使用相对路径')
        target=base.joinpath(*relative.parts)
        if not target.resolve().is_relative_to(base):raise ValueError('测试材料路径越界')
        return target

    def files(self, values, root=None):
        base=Path(root or self.root)
        for name,text in values.items():
            file=self.path(name,base);file.parent.mkdir(parents=True,exist_ok=True)
            file.write_text(text,encoding='utf8',newline='\n')
        return base

    def read(self, name):
        return self.path(name).read_bytes().decode('utf8')

    def prepared(self, name):
        """Return (directory, created); caller chooses when to rebuild its data."""
        base=os.environ.get('MODOT_TEST_ENV')
        if not base:raise EnvironmentBlocked('需要保留准备材料时，请首次传environment_id="new"，以后沿用返回ID')
        target=self.path(name,base)
        try:target.mkdir(parents=True);created=True
        except FileExistsError:created=False
        return target,created

    def check(self, condition, label='assertion', *, selector=None):
        self.phase='assert'
        selector = str(label if selector is None else selector)[:240]
        self.results.append(dict(name=str(label)[:240],passed=bool(condition),selector=selector))
        if not condition:print('WORKROOM_ASSERTION_FAILED: '+str(label),file=sys.stderr)
        return bool(condition)

    def equal(self, actual, expected, label='equal'):
        return self.check(actual==expected,str(label)+': '+repr(actual)[:200]+' == '+repr(expected)[:200],selector=label)

    def blocked(self, reason):
        raise EnvironmentBlocked(reason)

    def command(self, argv, cwd=None, timeout=60, expected_exit=0):
        self.phase='execute'
        argv=list(argv)
        if argv[0] in {'python','python3','python.exe'}:argv[0]=sys.executable
        try:
            # The caller's AppContainer/job still owns this entire process family.
            result=subprocess.run(argv,cwd=cwd or os.getcwd(),capture_output=True,text=True,
                                  encoding='utf8',errors='replace',timeout=timeout)
        except FileNotFoundError as error:raise EnvironmentBlocked(str(error)) from error
        print(result.stdout,end='');print(result.stderr,end='',file=sys.stderr)
        self.commands+=1
        if result.returncode != expected_exit:
            self.results.append(dict(name='子进程退出码 '+str(result.returncode)+'，预期 '+str(expected_exit),passed=False))
        return result

    def sphinx(self, files, config='project = "example"\nmaster_doc = "index"\n', builder='html'):
        src=self.root/'docs';out=self.root/'build'
        self.files(dict(files,**{'conf.py':config}),src)
        self.command(['python','-m','sphinx','-E','-b',builder,'-d',str(self.root/'doctree'),str(src),str(out)])
        return out

    def links(self, file):
        class Links(HTMLParser):
            def __init__(self):super().__init__();self.values=[]
            def handle_starttag(self,tag,attrs):
                if tag=='a':self.values.extend(v for k,v in attrs if k=='href')
        reader=Links();reader.feed(Path(file).read_text(encoding='utf8'));return reader.values


def _execute(payload):
    global case
    value=json.loads(payload)
    folder=Path(__file__).resolve().parent
    case=Case(folder/'case');case.root.mkdir()
    sys.modules['modot_testkit']=sys.modules[__name__]
    state='unknown';error=None
    try:
        case.files(value.get('files',{}));case.phase='execute'
        if value['runner']=='command':
            case.command(value['argv'])
        else:
            class Assertions(ast.NodeTransformer):
                def visit_Assert(self,node):
                    # Count only assertions that actually executed successfully.
                    observed=ast.Expr(ast.Call(ast.Name('_modot_observed_assert',ast.Load()),
                        [ast.Constant('assert line '+str(node.lineno))],[]))
                    return [node,ast.copy_location(observed,node)]
            tree=ast.fix_missing_locations(Assertions().visit(ast.parse(value['script'])))
            exec(compile(tree,value['name']+'.py','exec'),
                 dict(__name__='__scenario__',params=value.get('defaults',{}),case=case,
                      _modot_observed_assert=lambda label:case.check(True,label)))
        state='failed' if any(not r['passed'] for r in case.results) else 'passed' if case.results else 'unasserted'
    except EnvironmentBlocked as exc:state='blocked';error=str(exc);traceback.print_exc()
    except AssertionError as exc:state='failed';error=str(exc);traceback.print_exc()
    except BaseException as exc:
        state='preparation_failed' if case.phase=='prepare' or isinstance(exc,(ImportError,ModuleNotFoundError)) else 'error'
        error=str(exc);traceback.print_exc()
    report=dict(format=1,status=state,assertions=case.results,commands=case.commands,error=error,phase=case.phase)
    (folder/'workroom.json').write_text(json.dumps(report,ensure_ascii=False),encoding='utf8')
    print('MODOT_WORKROOM_RESULT: '+json.dumps(report,ensure_ascii=False))
    raise SystemExit(1 if state in {'failed','blocked','preparation_failed','error'} else 0)
