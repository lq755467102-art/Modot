"""MCP newline JSON-RPC in a no-network, private-copy AppContainer.

Only package-relative Python entry scripts are admitted. No shell, install,
host fallback, inherited credentials or access to the user's project directory.
"""
# 主要引用方包括 `plugin_packages.py` 和 `plugin_hooks.py`。
import json
from pathlib import Path
import queue
import threading
import time
from . import workbench as W, plugin_mcp as M, plugin_packages as P, safe_executor as X


def descriptor(root, config):
    if set(config)-{'command','args','type','disabled'} or config.get('command') not in {'python','python3','python.exe'}:
        raise W.Problem('plugin_stdio','当前隔离服务只支持无环境变量的包内 Python 入口',409)
    args=config.get('args',[])
    if not isinstance(args,list) or not args or len(args)>64 or any(not isinstance(a,str) or len(a)>4096 or '\0' in a for a in args):
        raise W.Problem('plugin_stdio','stdio 参数必须为明确的入口脚本和参数数组',409)
    script=args[0]
    for prefix in ('${CLAUDE_PLUGIN_ROOT}/','${CODEX_PLUGIN_ROOT}/','${PLUGIN_ROOT}/'):
        if script.startswith(prefix):script='./'+script[len(prefix):]
    if not script.startswith(('./','/')):script='./'+script
    target=P.inside(Path(root),script)
    if not target.is_file() or target.suffix.lower()!='.py':
        raise W.Problem('plugin_stdio','stdio 入口须为已安装包内的 Python 文件',409)
    return dict(command='python',args=[target.relative_to(root).as_posix(),*args[1:]])


# `plugin_runtime.py` 传入安装时核验的插件包和声明的进程目标。
# 继承 `plugin_mcp.py` 的协议行为，传输由本机子进程的输入输出承载。
class Client(M.Client):
    def __init__(self,package,target,validate_live=lambda:None):
        self.session='';self.protocol='2025-06-18';self.seq=0
        self.validate_live=validate_live
        root=Path(package['root'])
        if P.inspect(str(root))['fingerprint']!=package['fingerprint']:
            raise W.Problem('plugin_changed','本机插件包已变化，未执行',409)
        argv=['python',*target['process']['args']]
        def verify_copy(path):
            if P.inspect(str(path))['fingerprint']!=package['fingerprint']:
                raise W.Problem('plugin_changed','隔离插件副本与授权版本不一致',409)
        self.proc=X.start(argv,cwd=root,duplex=True,minimal_runtime=True,
                          time_limit=120,memory=256*1024*1024,processes=1,validate=validate_live,verify_copy=verify_copy)
        self.deadline=time.monotonic()+25
        self.messages=queue.Queue(maxsize=64)
        self.closed=False
        def read():
            total=0
            try:
                while True:
                    line=self.proc.stdout.stream.readline(M.MAX_RESPONSE+1)
                    if not line:raise EOFError('MCP stdio closed')
                    total+=len(line)
                    if len(line)>M.MAX_RESPONSE or total>8*M.MAX_RESPONSE:raise ValueError('MCP output capacity')
                    self.messages.put_nowait(line)
            except Exception as error:
                try:self.messages.put_nowait(error)
                except queue.Full:pass
                self.proc.kill()
        def drain():
            try:
                while self.proc.stderr.read1():pass
            except (OSError,ValueError):pass
        self.readers=[threading.Thread(target=read,daemon=True),threading.Thread(target=drain,daemon=True)]
        for thread in self.readers:thread.start()

    def request(self,method,params=None,notify=False):
        self.validate_live();self.seq+=1
        payload=dict(jsonrpc='2.0',method=method)
        if params is not None:payload['params']=params
        if not notify:payload['id']=self.seq
        raw=(json.dumps(payload,ensure_ascii=False)+'\n').encode()
        if len(raw)>128*1024:raise W.Problem('plugin_capacity','MCP 请求过大',413)
        sent=threading.Event();errors=[]
        def write():
            try:
                offset=0
                while offset<len(raw):
                    count=self.proc.stdin.write(raw[offset:])
                    if not count:raise EOFError('MCP stdin closed')
                    offset+=count
            except Exception as error:errors.append(error)
            finally:sent.set()
        writer=threading.Thread(target=write,daemon=True);writer.start()
        while not sent.wait(.05):self.check()
        if errors:raise W.Problem('plugin_unavailable','本机 MCP 请求未确认，未自动重放',502)
        if notify:return {}
        received=0
        while received<128:
            self.check()
            try:line=self.messages.get(timeout=.05)
            except queue.Empty:continue
            received+=1
            if isinstance(line,Exception):raise W.Problem('plugin_protocol','本机 MCP 回执不完整，未自动重放',502)
            try:
                result=self.reply(line)
            except (ValueError,TypeError,AttributeError) as error:
                raise W.Problem('plugin_protocol','本机 MCP 返回了非协议内容',502) from error
            if result is not None:return result
        raise W.Problem('plugin_protocol','本机 MCP 通知过多或未收到匹配回执',502)

    def check(self):
        self.validate_live()
        if time.monotonic()>self.deadline:
            self.proc.kill()
            raise W.Problem('plugin_timeout','本机 MCP 请求超时，未自动重放',504)

    def close(self):
        if self.closed:return
        self.closed=True
        self.proc.kill()
        for stream in (self.proc.stdin,self.proc.stdout,self.proc.stderr):
            if stream:
                try:stream.close()
                except (OSError,ValueError):pass
        for thread in self.readers:thread.join(timeout=2)
