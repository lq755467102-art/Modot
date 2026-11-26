"""Tool-owned model projections; canonical receipts remain audit evidence.

Execution and reported tests are observations, not task acceptance decisions.
"""
# 主要引用方包括 `executor_core.py` 和 `loop.py`。
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
import json

# Shared by initial projection and later history pruning. These are recorded
# facts, not acceptance decisions; reducing other payload must not erase them.
FACT_FIELDS = ('ok','error','code','needs_human','question','state','execution_state',
    'verification','diagnostics','revision','operationId','processId','execution','background','approvalRefs','memoryProvider','memoryWrite',
    '_harnessEvidence','_harnessProjection')
NATIVE_PAGE_POLICIES = dict.fromkeys(('search_project_code','inspect_code','read_project_resource'),'code')
NATIVE_PAGE_POLICIES.update(read_task_requirements='requirements',read_action_evidence='formatted')
NATIVE_PAGE_POLICIES.update(read_context_history='history',read_plugin_file='reference',read_plugin_skill='reference')


def legacy_json(result, args):
    return deepcopy(result)


def source_receipt(result, args):
    if not isinstance(result, dict):
        return result
    return {k:deepcopy(v) for k,v in result.items() if k not in {'timingsMs','operationCosts'}}


def plugin_receipt(result, args):
    """Only host metadata uses native parameter names; remote payload is opaque."""
    view=deepcopy(result)
    if not isinstance(view,dict):
        return view
    for old,new in (('schemaHash','schema_hash'),('catalogHash','catalog_hash')):
        if old in view:view[new]=view.pop(old)
    for row in view.get('tools',[]):
        if isinstance(row,dict) and 'schemaHash' in row:
            row['schema_hash']=row.pop('schemaHash')
    return view


def write_receipt(result, args):
    view = source_receipt(result, args)
    if not isinstance(view, dict):
        return view
    operation = view.get('operationId')
    if isinstance(operation, str) and operation:
        # Keep the target adjacent to its operation. The raw receipt and action
        # reference stay intact; a different backup/change identity is not an alias.
        view = {**{key:view[key] for key in ('operationId', 'path') if key in view},
                **{key:value for key,value in view.items() if key not in {'operationId', 'path'}}}
        for key in ('backupId', 'changeId'):
            if view.get(key) == operation:
                view.pop(key)
    return view


def log_receipt(result, args):
    if isinstance(result,dict) and any(isinstance(result.get(key),dict)
            for key in ('execution','verification','diagnostics')):
        return check_receipt(result,args)
    view=source_receipt(result,args)
    if isinstance(view,dict) and isinstance(view.get('process'),dict):
        view['process']={k:deepcopy(view['process'][k]) for k in
            ('id','workspaceId','state','exitCode','revision','retainedOutput') if k in view['process']}
    if isinstance(view,dict) and view.get('ok') is True:
        view.pop('ok')  # Reading succeeded; the process facts describe the command.
    return view


def summary_receipt(result, args):
    if not isinstance(result,dict) or not result.get('ok'):
        return deepcopy(result)
    return dict(ok=True,note='工作摘要已保存；适用记录由上下文提供，不代表验收通过。',
        record={k:result[k] for k in ('role','task','runId') if k in result},
        **{k:deepcopy(result[k]) for k in ('summaryVersion','correctionChanges') if k in result})


def _execution_facts(result):
    execution=deepcopy(result.get('execution') or {})
    recorded=result.get('verification') or {}
    if recorded.get('revision') is not None:
        if execution.get('sourceRevision') not in (None,recorded['revision']):
            execution['recordedRevision']=recorded['revision']
        else:
            execution.setdefault('sourceRevision',recorded['revision'])
    status=recorded.get('status')
    # Preserve exceptional evidence states and any discrepancy, without a
    # second ordinary passed/failed alias for execution or reported tests.
    if (status not in {None,'passed','failed','running'}
            or status=='failed' and execution.get('state')!='failed' and execution.get('exitCode') in (None,0)
            or status=='passed' and execution.get('state')!='completed' and execution.get('exitCode')!=0):
        execution['recordedState']=status
    return execution


def _diagnostic_facts(diagnostics):
    view=deepcopy(diagnostics)
    has_messages=any(key in view for key in ('keyErrors','environmentBlockers'))
    messages={}
    for entry in [*view.pop('keyErrors',[]),*view.pop('environmentBlockers',[])]:
        entry={key:value for key,value in entry.items() if key!='kind'}
        messages.setdefault((entry.get('line'),entry.get('after'),entry.get('text')),entry)
    if has_messages:
        view['messages']=sorted(messages.values(),key=lambda e:(e.get('line',0),e.get('after',0)))
    report=view.get('testReport')
    if isinstance(report,dict):
        report.pop('requirementAcceptance',None)
    return view


# `result` 是 `code_service.py` 或 `check_sessions.py` 的宿主回执，`args` 是 `tools.py` 交入的原工具参数。
# 这里只生成模型读取视图，原回执保留在宿主。
def command_summary(result):
    execution = result.get('execution') or {}
    state = ((result.get('diagnostics') or {}).get('testReport') or {}).get('state')
    if (execution.get('executed') and execution.get('state') in {'completed','failed'} and state == 'not_reported'
            and execution.get('exitCode') is not None and not execution.get('timedOut') and not execution.get('cancelled')
            and (result.get('verification') or {}).get('status') not in {'stale','unknown','blocked'}):
        return '命令退出码 ' + str(execution['exitCode']) + '。未报告测试用例。'
    return None


def check_receipt(result, args):
    if not isinstance(result,dict):
        return deepcopy(result)
    if not any(isinstance(result.get(key),dict) for key in ('diagnostics','execution','verification')) and not (
            result.get('background') is True and isinstance(result.get('process'),dict)):
        return deepcopy(result)
    view={k:deepcopy(v) for k,v in result.items()
        if k not in {'process','timingsMs','operationCosts','checkKey','executionEnvironment',
                     'verification','assessment'}}
    process=result.get('process') or {}
    execution=_execution_facts(result)
    if summary := command_summary(result):
        view['summary'] = summary
    if execution:
        view['execution']=execution
    if view.get('ok') is True or execution.get('executed') is True:
        view.pop('ok',None)
    if isinstance(view.get('diagnostics'),dict):
        view['diagnostics']=_diagnostic_facts(view['diagnostics'])
    if purpose := (result.get('purpose') or (result.get('assessment') or {}).get('purpose')):
        view['purpose']=purpose
    view['process']={k:deepcopy(process[k]) for k in
        ('id','workspaceId','state','exitCode','argv','retainedOutput','environmentId','outputFormat','started','finished','isolation') if k in process}
    if 'environment' in view:
        view['environmentFingerprint']=view.pop('environment')
    if process.get('environmentId'):
        view['environment_id']=process['environmentId']
    if result.get('workroom'):
        report=view['workroom'];assertions=report.pop('assertions',[])
        report['assertionCounts']=dict(passed=sum(bool(r.get('passed')) for r in assertions),
            failed=sum(not r.get('passed') for r in assertions))
        report['failedAssertions']=[r for r in assertions if not r.get('passed')][:3]
        retained=view['process'].get('retainedOutput',{})
        retained.pop('workroom',None)
        if not retained:view['process'].pop('retainedOutput',None)
    environment=result.get('executionEnvironment') or process.get('executionEnvironment')
    if environment:
        view['executionEnvironment']=deepcopy(environment)
    # 日志引用优先用已确认的进程 ID。后台启动尚无进程时可沿原操作 ID 查询，第一处失败偏移指向原日志。
    pid = execution.get('processId') or process.get('id') or result.get('processId')
    operation = result.get('operationId')
    if pid or operation and result.get('background'):
        pointer = dict(process_id=pid) if pid else dict(operation_id=operation)
        if args.get('resource_id'):
            pointer['resource_id']=args['resource_id']
        if result.get('background') and execution.get('state') in {'starting','running','stopping','finalizing'}:
            pointer['wait_ms']=30000
        elif type(offset := ((view.get('diagnostics') or {}).get('firstFailure') or {}).get('after')) is int and offset >= 0:
            pointer['after']=offset
        view['readOutput']=dict(tool='read_check_output', **pointer)
    if version := terminal_check_version(view):
        view['receiptVersion']=version
    return view


def terminal_check_version(view):
    """Identify confirmed check facts independently of a requested log page."""
    execution = view.get('execution') or {}
    process = view.get('process') or {}
    state = execution.get('state') or process.get('state')
    if not view.get('operationId') or state not in {'completed','failed','interrupted','cancelled','timed_out'}:
        return None
    if execution.get('exitCode', process.get('exitCode')) is None:
        return None
    fields = ('operationId','projectRevisionAfter','environmentFingerprint','purpose',
              'environmentBlocker','diagnostics','execution')
    facts = {key:view[key] for key in fields if key in view}
    facts['processState'] = state
    facts['exitCode'] = execution.get('exitCode', process.get('exitCode'))
    return sha256(json.dumps(facts,sort_keys=True,ensure_ascii=False).encode('utf8')).hexdigest()[:24]


NATIVE_RENDERERS={name:source_receipt for name in (
    'read_project_resource','inspect_code','search_project_code','edit_project_file',
    'write_project_file','create_project_file','delete_project_file')}
NATIVE_RENDERERS.update(dict.fromkeys(
    ('edit_project_file', 'write_project_file', 'create_project_file'), write_receipt))
NATIVE_RENDERERS.update(read_check_output=log_receipt,record_working_summary=summary_receipt,
    run_project_check=check_receipt)
NATIVE_RENDERERS.update(dict.fromkeys(('inspect_plugin_tools','call_plugin_tool'),plugin_receipt))


def install_native_contracts(registry):
    for name in registry.names():
        tool=registry.get(name)
        # An explicitly declared renderer (e.g. a plugin adapter) owns its view.
        if tool.result_renderer is None:
            tool.result_renderer=NATIVE_RENDERERS.get(name,legacy_json)
        if tool.result_page is None:
            tool.result_page=NATIVE_PAGE_POLICIES.get(name)


# `value` 是实际交给模型的视图，`metadata` 描述保留或裁剪情况，由调用方另外记录来源，不能混作工具事实。
@dataclass(frozen=True)
class Projection:
    value: object
    metadata: dict


def project(tool, result, args=None):
    """Render once after execution; presentation failures never replay a tool."""
    from .private_keys import clean
    result = clean(result)
    renderer=getattr(tool,'result_renderer',None) or legacy_json
    metadata=dict(version=1,renderer=getattr(renderer,'__name__','declared'),state='rendered')
    if getattr(tool,'result_renderer',None) is None:
        return Projection(result,metadata)  # Unmigrated callers retain their original JSON path.
    try:
        value=renderer(deepcopy(result),deepcopy(args or {}))
        json.dumps(value,ensure_ascii=False,allow_nan=False)
        # Protect factual failure, approval, version and diagnostic fields from
        # accidental renderer success/omission. No new acceptance inference.
        if isinstance(result,dict):
            # Native check views relocate recorded state and remove redundant
            # aliases. Compare their declared facts, not their old locations.
            # Third-party renderers still must preserve every protected field.
            facts=result
            if renderer in (check_receipt,log_receipt):
                facts=dict(result)
                check=any(isinstance(result.get(k),dict) for k in ('execution','verification','diagnostics'))
                if check:
                    facts.pop('verification',None)
                    execution=_execution_facts(result)
                    if execution:facts['execution']=execution
                    if isinstance(result.get('diagnostics'),dict):
                        facts['diagnostics']=_diagnostic_facts(result['diagnostics'])
                    if execution.get('executed') is True:facts.pop('ok',None)
                if facts.get('ok') is True and (renderer is log_receipt or check):
                    facts.pop('ok',None)
            for key in FACT_FIELDS:
                if key in facts and (not isinstance(value,dict) or value.get(key)!=facts[key]):
                    raise ValueError('projection changed factual field: '+key)
            process=result.get('process')
            if isinstance(process,dict):
                for key in ('state','exitCode'):
                    if key in process and (not isinstance(value,dict) or
                            (value.get('process') or {}).get(key)!=process[key]):
                        raise ValueError('projection changed process fact: '+key)
        return Projection(value,metadata)
    except Exception as error:
        metadata.update(state='fallback',errorType=type(error).__name__)
        value=deepcopy(result)
        notice=dict(state='fallback',errorType=type(error).__name__,
            note='仅结果呈现失败，不等于工具未执行；核对已记录的执行状态，不要仅因呈现失败重放工具。')
        if isinstance(value,dict):
            value['_harnessProjection']=notice
        try:
            # Existing legacy tools may return an opaque JSON leaf. Keep
            # factual status fields and report presentation loss separately.
            value=json.loads(json.dumps(value,ensure_ascii=False,allow_nan=False,default=str))
        except (TypeError,ValueError,RecursionError):
            value={'_harnessProjection':notice}
            if isinstance(result,dict):
                for key in ('ok','error','code','needs_human','execution_state','revision','operationId'):
                    try:
                        if key in result:
                            value[key]=json.loads(json.dumps(result[key],allow_nan=False,default=str))
                    except (TypeError,ValueError,RecursionError):pass
        return Projection(value,metadata)
