"""Regression seams from the real history audit, no vendor model calls."""
from copy import deepcopy
import io,json,urllib.error,unittest
from types import SimpleNamespace
from unittest.mock import patch

from loop import context_manager as C,context_view as V,history_compaction as H
from loop.context_inspection import request_owner
from loop.provider import Msg,Reply
from loop.tools import Tool,Registry
from tools import test_history_compaction as F
SUMMARY=F.SUMMARY


def tool_body(name,value):
    return C.RequestBody(dict(messages=[
        Msg('assistant','read',tool_calls=[dict(id='page',type='function',function=dict(name=name,arguments='{}'))]).to_dict(),
        Msg('tool',json.dumps(value,ensure_ascii=False),tool_call_id='page').to_dict()],tools=[]),{})


class BudgetAndSummary(unittest.TestCase):
    setUp=F.HistorySeam.setUp
    source=F.HistorySeam.source
    def test_small_model_sends_valid_input_without_sacrificing_output(self):
        C.bind_capacity(self.session,self.provider,32768)
        wire=[]
        def once(req,should_stop=None):
            wire.append(json.loads(req.data))
            return dict(choices=[dict(message=dict(content='OK'),finish_reason='stop')])
        with request_owner(self.session,'executor'),patch.object(self.provider,'_once',side_effect=once):
            result=self.provider.chat([Msg('system','rules'),Msg('user','say OK')],[])
        self.assertEqual(result.text,'OK');self.assertEqual(len(wire),1)
        self.assertEqual(wire[0]['max_tokens'],1024)
        policy=H.policy(self.session,self.provider,wire[0],131072)
        self.assertLess(policy['retainTokens'],policy['threshold'])

    def test_summary_overflow_reduces_real_wire_prefix_then_resumes(self):
        wire=[]
        def once(req,should_stop=None):
            body=json.loads(req.data);wire.append(body)
            if len(wire)==1:
                data=dict(error=dict(code='context_length_exceeded',context_window=131072,input_tokens=140000))
                raise urllib.error.HTTPError(req.full_url,400,'context too long',{},io.BytesIO(json.dumps(data).encode()))
            self.assertLess(len(body['messages']),len(wire[0]['messages']))
            C.validate_pairs(body['messages'])
            return dict(choices=[dict(message=dict(content=SUMMARY),finish_reason='stop')])
        source=self.source();before=deepcopy(source)
        with request_owner(self.session,'executor'),patch.object(self.provider,'_once',side_effect=once):
            result=C.prepare(self.provider,source)
        self.assertGreaterEqual(len(wire),2)
        self.assertEqual(len({C.fingerprint(b)for b in wire}),len(wire))
        self.assertEqual(source,before)
        self.assertIn('CURRENT_REAL_FAILURE',json.dumps(result))
        self.assertIn('LATEST_CORRECTION',json.dumps(result))
        self.assertEqual(result['tools'],source['tools'])

    def test_repeated_summary_overflow_is_bounded_and_never_publishes(self):
        wire=[]
        def once(req,should_stop=None):
            wire.append(json.loads(req.data))
            data=dict(error=dict(code='context_length_exceeded',context_window=131072,input_tokens=140000))
            raise urllib.error.HTTPError(req.full_url,400,'context too long',{},io.BytesIO(json.dumps(data).encode()))
        with request_owner(self.session,'executor'),patch.object(self.provider,'_once',side_effect=once):
            with self.assertRaises(Exception):C.prepare(self.provider,self.source())
        self.assertGreater(len(wire),1);self.assertLessEqual(len(wire),4)
        self.assertEqual(len({C.fingerprint(b)for b in wire}),len(wire))
        self.assertFalse(V.summary_path(self.session).exists())

    def test_native_publication_has_one_commit_file_and_failure_keeps_old_view(self):
        with request_owner(self.session,'executor'),patch.object(self.provider,'chat',return_value=Reply(SUMMARY)):
            C.prepare(self.provider,self.source())
        previous=V.summary_path(self.session).read_bytes()
        self.assertFalse(list((self.session.dir/'context-checkpoints').glob('p-*-latest.json')),
                         'native view must not publish a second independent pointer')
        row=json.loads(previous);self.assertEqual(len(row['checkpointId']),32)
        cp=json.loads((self.session.dir/'context-checkpoints'/('cp-'+row['checkpointId']+'.json')).read_text())
        self.assertEqual(row['candidateVersion'],cp['candidateVersion'])
        real=C.atomic_json
        def fail(path,value):
            if path==V.summary_path(self.session):raise OSError('summary publish failure')
            return real(path,value)
        new=self.source();new['messages'].extend(Msg('assistant','new facts '*6000).to_dict()for _ in range(12))
        with request_owner(self.session,'executor'),patch.object(self.provider,'chat',return_value=Reply(SUMMARY)),patch.object(C,'atomic_json',side_effect=fail):
            with self.assertRaises(C.ContextBlocked):C.prepare(self.provider,new,limit_evidence=dict(kind='api_context_limit'))
        self.assertEqual(V.summary_path(self.session).read_bytes(),previous)
        self.assertFalse(list((self.session.dir/'context-checkpoints').glob('p-*-latest.json')))
        fresh=SimpleNamespace(**vars(self.session));resumed=V.project(fresh,'executor',self.source())
        self.assertIn('<compacted-summary>',json.dumps(resumed))
        self.assertIn('CURRENT_REAL_FAILURE',json.dumps(resumed))

    def test_crash_after_single_commit_resumes_the_complete_candidate(self):
        real=C.atomic_json
        def crash_after_commit(path,value):
            real(path,value)
            if path==V.summary_path(self.session):raise SystemExit('process exited after fsync/replace')
        source=self.source()
        with request_owner(self.session,'executor'),patch.object(self.provider,'chat',return_value=Reply(SUMMARY)),patch.object(C,'atomic_json',side_effect=crash_after_commit):
            with self.assertRaises(SystemExit):C.prepare(self.provider,source)
        row=json.loads(V.summary_path(self.session).read_text())
        cp=json.loads((self.session.dir/'context-checkpoints'/('cp-'+row['checkpointId']+'.json')).read_text())
        self.assertEqual(row,cp['workSummary'])
        resumed=V.project(SimpleNamespace(**vars(self.session)),'executor',source)
        self.assertEqual(C.fingerprint(resumed),cp['candidateVersion'])
        self.assertIn('CURRENT_REAL_FAILURE',json.dumps(resumed))


class HistoryPageAndFacts(unittest.TestCase):
    def test_source_pagination_is_never_pruned_a_second_time(self):
        value=dict(ok=True,path='src/api.py',text='HEAD\n'+('precise source\n'*1200)+'def __eq__(self, other): return self.value == other.value\n',
                   startLine=1,endLine=1202,nextLine=1203,nextOffset=18000,hasMore=True,revision='v1')
        body=tool_body('inspect_code',value)
        result=V.prune_long_results(body)
        self.assertEqual(result,body);self.assertEqual(json.loads(result['messages'][-1]['content']),value)

    def test_search_page_cursor_and_failed_selector_remain_exact(self):
        value=dict(ok=True,matches=[dict(path='a.py',line=i,text='source '+('x'*180))for i in range(40)],
                   fileRevisions={'a.py':'v1'},nextCursor='EXACT_PAGE_CURSOR',truncated=True)
        body=tool_body('search_project_code',value)
        self.assertEqual(V.prune_long_results(body),body)

    def test_fact_fields_survive_aggregate_pruning_without_growing(self):
        facts=dict(ok=False,error='REAL_FAILURE',needs_human=True,question='Which product behavior?',
            execution_state='waiting_approval',approvalRefs=['actual-approval'],revision='v2',operationId='write-1',
            process=dict(id='p',state='exited',exitCode=1),readOutput=dict(tool='read_check_output',process_id='p'),
            diagnostics=dict(counts={'failed':1},failedCases=[dict(selector='tests/test_api.py::test_real',line=4)]),
            spill=dict(read=dict(tool='read_action_evidence',arguments=dict(action_id='a',run_id='r'))),
            _harnessEvidence=dict(actionId='a',runId='r'))
        raw=dict(facts,payload=[dict(text='padding')for _ in range(4000)])
        body=tool_body('call_plugin_tool',raw);before=deepcopy(body)
        result=V.prune_long_results(body);value=json.loads(result['messages'][-1]['content'])
        for key,v in facts.items():self.assertEqual(value.get(key),v,key)
        self.assertLess(len(json.dumps(result)),len(json.dumps(body)))
        self.assertEqual(body,before);C.validate_pairs(result['messages'])

    def test_large_diagnostics_stay_whole_even_without_pruning_gain(self):
        raw=dict(ok=False,diagnostics=dict(failedCases=[dict(selector='test_'+str(i),text='failure '*100)for i in range(100)]))
        body=tool_body('call_plugin_tool',raw);result=V.prune_long_results(body)
        self.assertEqual(result,body)

    def test_tool_declared_pagination_works_for_future_contributors(self):
        from loop.tool_results import install_native_contracts
        tool=Tool('read_extension_page','read context',{},lambda:None,zone='offflow',readonly=True,result_page='code')
        reg=Registry().scoped('task').add(tool)
        install_native_contracts(reg)
        value=dict(ok=True,path='plugin/source.txt',text='plugin source '*2000,startLine=1,endLine=2000,nextLine=2001,hasMore=True)
        body=tool_body(tool.name,value)
        self.assertEqual(V.prune_long_results(body,registry=reg),body)

    def test_requirements_and_formatted_retrieval_keep_exact_pages(self):
        for name,value in (
            ('read_task_requirements',dict(ok=True,text='exact user requirement '*450,next=9000,hasMore=True,revision='demand-v1')),
            ('read_action_evidence',dict(ok=True,format='model-json-v1',text='exact original output '*1400,next=30000,hasMore=True,evidenceRevision='saved-v1')),
            ('read_action_evidence',dict(ok=True,text='exact raw receipt '*1800,next=30000,hasMore=True,evidenceRevision='raw-v1')),
            ('read_context_history',dict(ok=True,text='"\\'*2000,nextOffset=4000,hasMore=True,messageVersion='message-v1',observedFields={'ok':False})),
            ('read_plugin_file',dict(ok=True,text='plugin source '*550,next=8000,hasMore=True,hash='plugin-file-v1')),
        ):
            with self.subTest(name=name):
                body=tool_body(name,value)
                self.assertEqual(V.prune_long_results(body),body)


class PluginContextBoundary(unittest.TestCase):
    setUp=F.HistorySeam.setUp

    def test_declared_plugin_snapshot_reaches_actual_wire_without_host_override(self):
        from loop.runtime_facts import RuntimeFacts
        plugin=dict(id='f'*32,fingerprint='installed-v1',revision=3,
            runtimeFacts=[dict(name='permissions',value=dict(mode='full',note='plugin claim only'))])
        facts=RuntimeFacts().register('host.permissions',lambda:dict(mode='ask'),source='host').plugin(plugin)
        self.session.runtime_facts=facts.collect(dict(taskId=self.session.task_input['id']))
        wire=[]
        def once(req,should_stop=None):
            wire.append(json.loads(req.data))
            return dict(choices=[dict(message=dict(content='OK'),finish_reason='stop')])
        with request_owner(self.session,'executor'),patch.object(self.provider,'_once',side_effect=once):
            self.provider.chat([Msg('system','HOST_RULES'),Msg('user','ACTUAL_REQUIREMENT')],[])
        self.assertEqual(len(wire),1)
        self.assertEqual(wire[0]['messages'][0],dict(role='system',content='HOST_RULES'))
        note=next(m for m in wire[0]['messages']if '[context-update:runtime-facts:'in str(m.get('content','')))
        self.assertEqual(note['role'],'user')
        value=json.loads(note['content'].split('\n',2)[2])['facts']
        self.assertEqual(value['host.permissions']['value']['mode'],'ask')
        declared=value['plugin.'+'f'*32+'.permissions']
        self.assertEqual(declared['trust'],'plugin-declaration')
        self.assertEqual(declared['source']['revision'],3)
        self.assertNotIn('context_assembly',wire[0])


if __name__=='__main__':unittest.main(verbosity=2)
