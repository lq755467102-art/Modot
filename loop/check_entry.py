"""Recognize a small, non-executed subset of Python test entry wrappers."""
# 主要引用方包括 `execution_completion.py`。
import ast


def django_wrapper(source):
    """Accept import/path setup + literal sys.argv + one final runpy call.

    No exec/eval, monkeypatches, conditional assertions, or trailing code.
    A successful process receipt is still required by the caller.
    """
    try:
        nodes = ast.parse(source).body
    except (SyntaxError, ValueError):
        return None
    argv = None
    imports = set()
    def text(node):
        return ast.unparse(node)
    def literal(node):
        try: return ast.literal_eval(node)
        except (ValueError, TypeError): return None
    for index, node in enumerate(nodes):
        if isinstance(node, ast.Import):
            if any(a.name not in {'sys','os','runpy'} or a.asname for a in node.names):return None
            imports.update(a.name for a in node.names)
            continue
        if isinstance(node, ast.Assign) and len(node.targets)==1:
            target=text(node.targets[0])
            if target=='sys.argv' and argv is None:
                argv=literal(node.value)
                if not isinstance(argv,list) or not argv or any(not isinstance(a,str) for a in argv):return None
            elif target=="os.environ['DJANGO_SETTINGS_MODULE']" and literal(node.value)=='test_sqlite':
                pass
            elif target=="os.environ['PYTHONPATH']" and text(node.value)=="os.path.abspath('tests')":
                pass
            else:return None
            continue
        if not isinstance(node,ast.Expr) or not isinstance(node.value,ast.Call):return None
        call=node.value
        if text(call.func)=='sys.path.insert' and not call.keywords and len(call.args)==2:
            if literal(call.args[0])!=0 or text(call.args[1]) not in {"os.path.abspath('tests')", "os.path.abspath('.')", "'tests'"}:return None
            continue
        if (index==len(nodes)-1 and text(call.func)=='runpy.run_path' and argv is not None
                and {'sys','runpy'}<=imports and len(call.args)==1
                and text(call.args[0]) in {"'tests/runtests.py'", "os.path.join('tests', 'runtests.py')"}
                and len(call.keywords)==1 and call.keywords[0].arg=='run_name'
                and literal(call.keywords[0].value)=='__main__'):
            return ['python','tests/runtests.py',*argv[1:]]
        return None
    return None
