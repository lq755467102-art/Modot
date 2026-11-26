"""Fixed parser workers get only an input copy, trusted code and dependencies."""
# 主要引用方包括 `documents.py` 和 `image_processing.py`。
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
from . import workbench as W, safe_executor as X


# 包装 `safe_executor.py` 创建的原进程。
# 后台线程持续读完管道，`result` 或 `error` 随 `done` 发布给调用方。
class ParserProcess:
    isolation = 'appcontainer-parser'

    def __init__(self, process):
        self.process = process
        self.done = threading.Event()
        self.result = self.error = None
        def collect():
            try:
                self.result = process.communicate()
            except Exception as error:
                self.error = error
            finally:
                self.done.set()
        threading.Thread(target=collect, daemon=True).start()

    def __getattr__(self, name):
        return getattr(self.process, name)

    def communicate(self, input=None, timeout=None):
        if not self.done.wait(timeout):
            raise subprocess.TimeoutExpired('isolated parser', timeout)
        if self.error:
            raise self.error
        return self.result


# 文档入口 `documents.py` 或图片入口 `image_processing.py` 传入固定解析种类和已绑定哈希的源文件。
# 子进程只得到内容副本、所选解析器和依赖。
def start(kind, request):
    if kind not in {'document', 'image', 'video'}:
        raise ValueError('Unknown fixed worker')
    with Path(request['path']).open('rb') as stream:
        raw = stream.read(2 * 1024 * 1024 + 1)
    if len(raw)>2*1024*1024 or hashlib.sha256(raw).hexdigest()!=request['hash']:
        raise W.Problem('parser_source', '解析输入已变化，未运行', 409)
    with tempfile.TemporaryDirectory(prefix='harness-parser-source-') as directory:
        root = Path(directory)
        (root/'input').write_bytes(raw)
        (root/'request.json').write_text(json.dumps(dict(request,path='input')), encoding='utf8')
        worker = kind+'_worker.py'
        shutil.copyfile(W.P.app_root()/'loop'/worker, root/worker)
        if kind == 'image':
            dependencies = ('PIL',)
        elif kind == 'video':
            dependencies = ('av', 'PIL')
        else:
            dependencies = {'pdf': ('pypdf',), 'xlsx': ('openpyxl', 'et_xmlfile', 'defusedxml')}.get(request.get('format'), ())
        for dependency in dependencies:
            spec = importlib.util.find_spec(dependency)
            if spec is None or not spec.submodule_search_locations:
                raise W.Problem('parser_dependency', '缺少随程序安装的解析依赖', 503)
            package = Path(next(iter(spec.submodule_search_locations)))
            X._copy(package, root/dependency)
            if dependency == 'av' and (package.parent/'av.libs').is_dir():
                X._copy(package.parent/'av.libs', root/'av.libs')
        process = X.start(['python',worker,'request.json'],cwd=root,
                          memory=256*1024*1024,processes=1,time_limit=8,minimal_runtime=True)
    return ParserProcess(process)
