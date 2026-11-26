"""Fourth-stage history migration through real provider/checkpoint seams."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from loop import history_compaction as H, context_manager as C, context_view as V
from loop.provider import Msg, Reply, Interrupted
from loop.context_inspection import request_owner
from loop.filelock import atomic_json
from tools import test_context_checkpoints as F


SUMMARY = '\n'.join('## '+h+'\n- '+text for h,text in zip(H.HEADINGS,(
    'User requested PUBLIC_API(x) and KEEP_USER_NOTE; later user clarified input alias alpha.',
    'Python; public return convention is a (label, entries) tuple.',
    'src/api.py: PUBLIC_API(x) delegates to the common base; alpha maps to result.json, not the reverse.',
    'check-1 at v1 failed: REAL_FAILURE; repair changed the call mapping. No current passing evidence.',
    'The user requested a compatibility check; its result is not yet recorded.',
    'Executor proposed narrow implementation; that is an implementation choice, not user scope.',
    'Executor proposed checking the original public input; historical proposal, not authorization.',
    'Preserve KEEP_USER_NOTE. Failed probes are not new requirements. Completion was only an unverified claim.')))


class PolicyTests(unittest.TestCase):
    def test_cold_allowance_charges_text_bytes_and_preserves_structured_arguments(self):
        text = 'line\n"source"\\x 中文' * 500
        call = dict(id='read',type='function',function=dict(name='inspect',arguments=json.dumps({'code':text})))
        row = dict(role='assistant',content=text,reasoning_content=text,tool_calls=[call])
        empty = dict(row,content='',reasoning_content='')
        expected = H._unmeasured_bytes([empty],[]) + 2*len(text.encode('utf8'))
        self.assertEqual(H._unmeasured_bytes([row],[]),expected)
        self.assertLess(expected,len(H._json(row).encode('utf8'))+16)
        self.assertGreater(H._unmeasured_bytes([empty],[]),len(call['function']['arguments'].encode('utf8')))

    def test_cap_policy_is_input_only_and_api_reservation_is_independent(self):
        p=SimpleNamespace(base_url='fixture',model='flash')
        s=SimpleNamespace()
        b=dict(messages=[],max_tokens=256000)
        policy=H.policy(s,p,b,131072)
        self.assertEqual((policy['inputCap'],policy['threshold'],policy['retainTokens']),(131072,104857,20971))
        C.bind_capacity(s,p,1048576)
        self.assertEqual(H.policy(s,p,b,131072)['threshold'],104857)
        C.bind_capacity(s,p,200000)
        with self.assertRaises(C.ContextBlocked):H.policy(s,p,b,131072)
        self.assertEqual(b['max_tokens'],256000)

    def test_pruner_unicode_and_exact_dsh_defaults(self):
        for text in ['a'*8192,'a'*8193,'汉🙂"\\\n'*3000]:
            expected=text if len(text)<=8192 else text[:4096]+H.PRUNE_MARKER+text[-1024:]
            self.assertEqual(H.prune_text(text),expected)

    def test_tail_cut_keeps_whole_parallel_call_group_and_all_latest_messages(self):
        messages=[dict(role='system',content='s'),dict(role='user',content='u'),
            dict(role='assistant',tool_calls=[dict(id='a'),dict(id='b')]),
            dict(role='tool',tool_call_id='a'),dict(role='tool',tool_call_id='b'),dict(role='user',content='new')]
        self.assertEqual(H.range_end(messages,[1]*6,2),2)
        self.assertEqual(H.range_end(messages,[1]*6,0),5)
        self.assertIsNone(H.range_end(messages,[1]*6,99))


class HistorySeam(unittest.TestCase):
    def setUp(self):
        f=F.CheckpointTests();f.setUp();self.addCleanup(f.doCleanups)
        self.session=f.session;self.provider=f.provider;self.events=f.events
        self.session.executor_core=object();self.session.task_input=dict(id='history-task')
        self.session.request_context_limit=131072
        self.provider.count_input_tokens=lambda body:H.measure(body)['tokens']
        p=patch('loop.projects.scope',return_value=dict(resources=[]));p.start();self.addCleanup(p.stop)
        p=patch('loop.memory_runtime.prepare_body',side_effect=lambda s,r,b:b);p.start();self.addCleanup(p.stop)

    def source(self):
        messages=[Msg('system','LIVE_PERMISSION: no wider write authority'),
            Msg('user','USER_ORIGINAL: PUBLIC_API(x), KEEP_USER_NOTE; original input alpha.')]
        for i in range(18):
            messages += [Msg('assistant',f'observed work {i}: '+('precise code fact '*1700)),
                Msg('assistant','read interface',tool_calls=[dict(id='r'+str(i),type='function',function=dict(name='inspect_code',arguments='{}'))]),
                Msg('tool',json.dumps(dict(ok=False,error='REAL_FAILURE',revision='v1',text='PUBLIC_API returns (label, entries)')),
                    tool_call_id='r'+str(i))]
        messages += [Msg('user','LATEST_CORRECTION: alpha remains the public input, preserve users edits.'),
            Msg('assistant','current check',tool_calls=[dict(id='current',type='function',function=dict(name='run_project_check',arguments='{"script":"assert actual == expected"}'))]),
            Msg('tool','{"ok":false,"error":"CURRENT_REAL_FAILURE","revision":"v2"}',tool_call_id='current')]
        return self.provider._body(messages,[dict(name='inspect_code',description='read',parameters={'type':'object'})])

    def prepare(self,body,reply=None,**kw):
        with request_owner(self.session,'executor'),patch.object(self.provider,'chat',return_value=reply or Reply(SUMMARY)):
            return C.prepare(self.provider,body,**kw)

    def test_actual_summary_prefix_wire_candidate_and_resume_have_correct_sources(self):
        body=self.source();original=deepcopy(body);wire=[]
        def once(req,should_stop=None):
            sent=json.loads(req.data);wire.append(sent)
            self.assertEqual(sent['messages'][-1],dict(role='user',content=H.SUMMARY_INSTRUCTION))
            self.assertEqual(sent['messages'][:-1],body['messages'][:len(sent['messages'])-1])
            self.assertEqual(sent['tools'],body['tools'])
            self.assertNotIn('LATEST_CORRECTION',json.dumps(sent))
            return dict(choices=[dict(message=dict(content=SUMMARY),finish_reason='stop')],usage=dict(prompt_tokens=10000,completion_tokens=400))
        with request_owner(self.session,'executor'),patch.object(self.provider,'_once',side_effect=once):
            result=C.prepare(self.provider,body)
        self.assertEqual(body,original);self.assertEqual(len(wire),1)
        self.assertEqual(result['messages'][0],body['messages'][0])
        text=json.dumps(result)
        self.assertNotIn('USER_ORIGINAL:',text,'old user retires from model view, not archive')
        self.assertIn('PUBLIC_API(x)',text);self.assertIn('alpha maps to result.json',text)
        self.assertIn('CURRENT_REAL_FAILURE',text);self.assertIn('LATEST_CORRECTION',text)
        summary_index=next(i for i,m in enumerate(result['messages']) if '<compacted-summary>' in str(m.get('content','')))
        correction=next(i for i,m in enumerate(result['messages']) if 'LATEST_CORRECTION' in str(m.get('content','')))
        self.assertLess(summary_index,correction)
        saved=next((self.session.dir/'context-checkpoints').glob('cp-*.json'))
        row=json.loads(saved.read_text());self.assertEqual(row['version'],1)
        self.assertEqual(row['workSummary']['version'],3);self.assertIn(1,row['handoff']['retiredUserIndices'])
        self.assertIn(row['id'],text);self.assertEqual(row['workSummary']['archive']['user_message_indices'],row['handoff']['retiredUserIndices'])
        self.assertEqual(row['original']['messages'],body['messages'])
        self.assertIn('not a new user instruction',text)
        fresh=SimpleNamespace(**vars(self.session))
        resumed=V.project(fresh,'executor',self.source())
        self.assertEqual(resumed['messages'],result['messages']);C.validate_pairs(result['messages'])
        self.assertEqual(row['handoff']['policy']['inputCap'],131072)

    def test_summary_preflight_prices_the_serialized_authorized_page(self):
        from loop.code_read_receipts import _binding
        body = self.source()
        directory = self.session.dir.resolve()
        resource = dict(id='source',kind='code',path=str(directory),permission='read')
        scope = dict(projectId=None,resources=[resource],lastRun=dict(resources=[resource]))
        page = next(m for m in body['messages'] if m['role']=='tool')
        value = json.loads(page['content'])
        value['_harnessCodePage'] = dict(sessionId=directory.name,resourceId='source',
                                         binding=_binding(None,resource))
        page['content'] = json.dumps(value,ensure_ascii=False)
        counted = []
        def count(request):
            if request['messages'][-1].get('content') == H.SUMMARY_INSTRUCTION:
                counted.append(C.fingerprint(request))
            return H.measure(request)['tokens']
        self.provider.count_input_tokens = count
        def send(req, should_stop=None):
            wire = json.loads(req.data)
            self.assertEqual(counted[-1],C.fingerprint(wire))
            sent_page = next(m for m in wire['messages'] if m['role']=='tool')
            self.assertNotIn('_harnessCodePage',sent_page['content'])
            self.assertIn('PUBLIC_API returns',sent_page['content'])
            return dict(choices=[dict(message=dict(content=SUMMARY),finish_reason='stop')])
        with request_owner(self.session,'executor'),patch('loop.projects.scope',return_value=scope), \
                patch('loop.subtasks.validate_child',return_value=None), \
                patch.object(self.provider,'_once',side_effect=send):
            C.prepare(self.provider,body)
        self.assertTrue(counted)
        self.assertIn('_harnessCodePage',page['content'])

    def test_below_pressure_no_model_call_no_history_rewrite(self):
        b=self.provider._body([Msg('system','rules'),Msg('user','original')],[])
        with request_owner(self.session,'executor'),patch.object(self.provider,'chat') as chat:
            result=C.prepare(self.provider,b)
        chat.assert_not_called();self.assertEqual(result,b)

    def test_new_task_reuses_history_but_not_old_task_state_or_changed_scope(self):
        from loop.working_context import state_snapshot_content
        source=self.source()
        marker='ONLY_THE_PREVIOUS_TASK'
        text=state_snapshot_content('executor-work',{'taskId':marker,'checks':{},'issues':{}})
        source['messages'].insert(2,dict(role='user',content=text))
        source.parts[2]=(('runtimeUpdate',text),)
        old=self.prepare(source)
        previous=old.work_summary
        self.assertIn(marker,previous['runtime']['executor-work'])
        self.session.task_input={'id':'another-task'}
        source['messages'].append(dict(role='user',content='A distinct request'))
        resumed=V.project(self.session,'executor',source)
        self.assertIn(previous['archive']['checkpoint_id'],json.dumps(resumed))
        self.assertIn('A distinct request',json.dumps(resumed))
        self.assertNotIn(marker,json.dumps(resumed))
        with patch('loop.projects.scope',return_value=dict(resources=[dict(id='different',permission='read')])):
            self.assertIsNone(V.checkpoint(self.session,source))
        source['messages'][1]['content']='replaced history'
        self.assertIsNone(V.checkpoint(self.session,source))

    def test_changing_only_the_tool_catalog_preserves_measured_prefix_pressure(self):
        del self.provider.count_input_tokens
        body=self.provider._body([Msg('system','rules'),Msg('user','history')],[])
        with request_owner(self.session,'executor'):
            C.record_usage(self.provider,body,dict(prompt_tokens=130000))
            expanded=C.RequestBody(dict(body,tools=[dict(type='function',function=dict(
                name='history',description='new schemas '*1500,parameters={}))]),{})
            self.assertGreater(H.measured(self.provider,expanded)['tokens'],131072)

    def test_cold_resume_uses_actual_count_only_for_matching_rows_and_model(self):
        del self.provider.count_input_tokens
        body=self.provider._body([Msg('system','rules'),Msg('user','known bytes '*14000)],[])
        with request_owner(self.session,'executor'):
            self.assertGreaterEqual(H.measured(self.provider,body,131072)['tokens'],131072)
            C.record_usage(self.provider,body,dict(prompt_tokens=42000))
        meter=self.session.dialogue_input_meter
        self.assertNotIn('known bytes',json.dumps(meter))
        fresh=SimpleNamespace(dir=self.session.dir,load=lambda:dict(dialogueInputMeter=meter))
        provider=SimpleNamespace(base_url=self.provider.base_url,model=self.provider.model)
        with request_owner(fresh,'executor'):
            resumed=dict(body,messages=[*body['messages'],dict(role='user',content='new task')])
            self.assertLess(H.measured(provider,resumed,131072)['tokens'],131072)
            doubled=dict(body,messages=[*body['messages'],body['messages'][1]])
            self.assertGreaterEqual(H.measured(provider,doubled,131072)['tokens'],131072)
            provider.model='different'
            self.assertGreaterEqual(H.measured(provider,resumed,131072)['tokens'],131072)

    def test_uncounted_long_history_retires_disjoint_spans_within_the_cap(self):
        del self.provider.count_input_tokens
        body=self.provider._body([Msg('system','rules'),Msg('user','old request')]
            +[Msg('assistant',f'part {i} '+('engineering log '*600)) for i in range(48)]
            +[Msg('user','current request')],[])
        sizes=[]
        def summarize(messages,tools,**kwargs):
            request=self.provider._body(messages,tools)
            sizes.append(H._unmeasured_bytes(request['messages'],request.get('tools',[]))+32)
            return Reply(SUMMARY)
        with request_owner(self.session,'executor'),patch.object(self.provider,'chat',side_effect=summarize):
            wire=C.prepare(self.provider,body)
        self.assertGreater(len(sizes),2)
        self.assertLess(max(sizes),131072)
        self.assertLess(H._unmeasured_bytes(wire['messages'],wire.get('tools',[]))+32,131072)
        self.assertEqual(wire['messages'][-1]['content'],'current request')

    def test_returned_usage_anchor_prices_new_history_instead_of_freezing_pressure(self):
        del self.provider.count_input_tokens
        b=self.provider._body([Msg('system','rules'),Msg('user','original')],[])
        with request_owner(self.session,'executor'):
            C.record_usage(self.provider,b,dict(prompt_tokens=123))
            self.assertEqual(H.measured(self.provider,b)['tokens'],123)
            extended=C.RequestBody(dict(b,messages=[*b['messages'],dict(role='assistant',content='new facts '*10000)]),{})
            grown=H.measured(self.provider,extended)
            self.assertGreater(grown['tokens'],20000)
            self.assertEqual(grown['tokens'],123+H.message_tokens(extended['messages'][-1]))

    def test_summary_can_price_a_shorter_previous_wire_prefix_even_after_restart(self):
        del self.provider.count_input_tokens
        source = self.provider._body([Msg('system','rules'), Msg('user','contract '*26500)], [
            dict(name='inspect', parameters={'type':'object'})])
        more = dict(source,messages=[*source['messages'],dict(role='assistant',content='newer page '*4000)])
        probe = dict(source,messages=[*source['messages'],dict(role='user',content=H.SUMMARY_INSTRUCTION)])
        with request_owner(self.session,'executor'):
            C.record_usage(self.provider,source,{'prompt_tokens':80214})
            C.record_usage(self.provider,more,{'prompt_tokens':104752})
            meter = deepcopy(self.session.dialogue_input_meter)
        resumed = SimpleNamespace(dir=self.session.dir,executor_core=object(),
                                  load=lambda: {'dialogueInputMeter':meter})
        fresh = SimpleNamespace(model=self.provider.model,base_url=self.provider.base_url)
        with request_owner(resumed,'executor'):
            price = H.measured(fresh,probe,131072)
            self.assertGreater(price['tokens'],80214)
            self.assertLess(price['tokens'],104857)
            self.assertFalse(price['exact'])
            self.assertEqual(H.measured(fresh,source,131072)['tokens'],80214)
            self.assertTrue(H.measured(fresh,source,131072)['exact'])
            for changed in [dict(source,tools=[]),
                            dict(source,messages=[dict(role='system',content='other'),source['messages'][1]])]:
                self.assertFalse(H.measured(fresh,changed,131072)['exact'])
            doubled = dict(probe,messages=[*probe['messages'],source['messages'][1]])
            self.assertGreaterEqual(H.measured(fresh,doubled,131072)['tokens'],131072)
            C.record_usage(fresh,source,{'prompt_tokens':80214})
            # A newer, shorter call must not mask an older exact match.
            self.assertEqual(H.measured(fresh,more,131072),
                             dict(tokens=104752,source='provider_usage',exact=True))
            resumed.context_summary_active = True
            C.record_usage(fresh,probe,{'prompt_tokens':80704})
            extra = dict(role='user',content='one more message')
            growth = H.measured(fresh,dict(probe,messages=[*probe['messages'],extra]),131072)
            self.assertEqual(growth['tokens'],80704+H.message_tokens(extra))

    def test_legacy_checkpoint_keeps_old_user_and_projection_semantics(self):
        b=self.source();source=V.project(self.session,'executor',b);cutoff=20
        while source['messages'][cutoff-1].get('tool_calls'):cutoff-=1
        row=dict(version=2,scope=V.scope_key(self.session),sourceCount=cutoff,
                 sourceVersion=V.prefix_version(source,cutoff),text='## User intent and corrections\nFAKE_USER_INTENT\n## Confirmed implementation decisions and reasons\nLEGACY_REASON',runtime={},toolResults={})
        atomic_json(V.summary_path(self.session),row);before=V.summary_path(self.session).read_bytes()
        result=V.project(SimpleNamespace(**vars(self.session)),'executor',b)
        self.assertIn('USER_ORIGINAL:',json.dumps(result));self.assertIn('LEGACY_REASON',json.dumps(result))
        self.assertNotIn('FAKE_USER_INTENT',json.dumps(result));C.validate_pairs(result['messages'])
        self.assertEqual(V.summary_path(self.session).read_bytes(),before)

    def test_summary_output_is_not_a_completion_gate_or_heading_classifier(self):
        result=self.prepare(self.source(),Reply('Executor claimed done; tests remain unverified.'))
        self.assertIn('claimed done',json.dumps(result));self.assertNotIn('completed',[e.get('state') for e in self.events if e['act']=='runtime'])

    def test_invalid_or_cancelled_summary_keeps_history_and_committed_pointer(self):
        body=self.source();self.prepare(body)
        previous=V.summary_path(self.session).read_bytes()
        for reply in [Reply(''),Reply(SUMMARY,finish_reason='length'),Reply(SUMMARY,tool_calls=[dict(name='edit',args={})])]:
            # Force a fresh same-task source with substantially new old history.
            atomic_json(V.summary_path(self.session),dict(version=2,scope='wrong'))
            with self.assertRaises(C.ContextBlocked):self.prepare(body,reply)
            self.assertEqual(json.loads(V.summary_path(self.session).read_text())['scope'],'wrong')
        V.summary_path(self.session).write_bytes(previous)
        before=deepcopy(body)
        with request_owner(self.session,'executor'),self.assertRaises(Interrupted):
            C.prepare(self.provider,body,should_stop=lambda:True,limit_evidence=dict(kind='api_context_limit'))
        self.assertEqual(body,before);self.assertEqual(V.summary_path(self.session).read_bytes(),previous)

    def test_no_gain_summary_is_not_published(self):
        before=self.source()
        with self.assertRaises(C.ContextBlocked):self.prepare(before,Reply('unchanged '*90000))
        self.assertFalse(V.summary_path(self.session).exists())

    def test_auxiliary_prefix_that_fits_can_need_a_second_bounded_summary(self):
        self.provider.count_input_tokens=lambda body:(len(json.dumps(body,ensure_ascii=False))+1)//2
        b=self.provider._body([Msg('system','rules'),Msg('user','original request')]+[
            Msg('assistant','precise source fact '*1600) for _ in range(18)]+[Msg('user','latest correction')],[])
        with request_owner(self.session,'executor'),patch.object(self.provider,'chat',return_value=Reply(SUMMARY)) as chat:
            result=C.prepare(self.provider,b)
        self.assertEqual(chat.call_count,2)
        self.assertLess(self.provider.count_input_tokens(result),104857)
        self.assertEqual(len(list((self.session.dir/'context-checkpoints').glob('cp-*.json'))),1)
        C.validate_pairs(result['messages'])


from tools.test_conversations import ConversationFixture


class RetiredUserRead(ConversationFixture,unittest.TestCase):
    def test_real_new_checkpoint_allows_retired_original_but_never_system_or_unretired_user(self):
        from loop import context_archive as A,projects,workbench as W
        from loop.tools import set_call,reset_call
        from loop.provider import OpenAICompatProvider
        _,thread=self.call('POST');ident=thread['id'];directory=self.root/ident
        root=W.workspace(projects.conversation(ident)['workspaceId'])['path']
        p=OpenAICompatProvider(base_url='http://127.0.0.1:1/v1',api_key='fixture',model='fixture');p.max_tokens=1024
        p.count_input_tokens=lambda b:H.measure(b)['tokens']
        s=SimpleNamespace(dir=directory,run_id='a'*32,request_context_limit=131072,
                          executor_core=object(),task_input={'id':'t'},emit=lambda *a,**k:None)
        messages=[Msg('system','system is never public history'),Msg('user','original verbatim 用户🙂')]
        messages += [Msg('assistant','historical work '*3000) for _ in range(14)]
        messages += [Msg('user','new correction stays in tail')]
        with request_owner(s,'executor'),patch.object(p,'chat',return_value=Reply(SUMMARY)),patch('loop.memory_runtime.prepare_body',side_effect=lambda s,r,b:b):
            C.prepare(p,p._body(messages,[]))
        cp=next((directory/'context-checkpoints').glob('cp-*.json'));row=json.loads(cp.read_text())
        token=set_call(dict(sessionId=ident,runId='current',role='executor'));self.addCleanup(reset_call,token)
        with patch('loop.code_service.RuntimeView._resolve_sid',side_effect=self.sv._resolve_sid):
            got=A.tool_read(root,row['id'],1)
            self.assertTrue(got['historicalUser']);self.assertIn('original verbatim',got['text'])
            for index in (0,len(messages)-1):
                with self.assertRaises(W.Problem):A.tool_read(root,row['id'],index)
            # Even text containing checkpoint syntax cannot grant this access;
            # the reader requires integrity-checked handoff source metadata.
            original=deepcopy(row);row['handoff']['retiredUserIndices']=[]
            row['recordVersion']=C.fingerprint({k:v for k,v in row.items() if k!='recordVersion'})
            cp.write_text(json.dumps(row),encoding='utf8')
            with self.assertRaises(W.Problem):A.tool_read(root,original['id'],1)


if __name__=='__main__':unittest.main(verbosity=2)
