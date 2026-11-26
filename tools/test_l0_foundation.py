"""Native L0 lifecycle regressions: no coordinator, workers, or model network."""
import unittest
import json
from unittest.mock import patch

from loop.loop import run
from loop.provider import MockProvider
from loop.tools import Registry
from loop import context_manager as CM, executor_core as EC
from loop import execution_policy as E
from loop import task_input
from tools import test_context_modes as modes
from tools.test_conversations import ConversationFixture


class L0LifecycleTests(ConversationFixture, unittest.TestCase):
    owner = modes.ContextModeTests.owner

    def execute(self, response, verify=None):
        _, root, session, provider, _ = self.owner('l0', calls=0)
        session.task_input = dict(id='task', revision=1, kind='new')
        with patch.object(provider, '_once', return_value=response) as request, \
                patch('loop.code_service.executor_verification', return_value=None):
            result = run('Keep constraints; report unverified work honestly', provider, Registry(),
                verify or (lambda: dict(ok=True)), session=session, executor_only=True, max_turns=2)
        return result, request.call_count, session

    @staticmethod
    def response(finish='stop'):
        return dict(choices=[dict(message=dict(content='Implementation saved; a check failed and remains unverified.'),
                                 finish_reason=finish)])

    def test_direct_loop_allows_more_than_eight_turns_by_default(self):
        script = [dict(tool="observe", args=dict(step=step, material="x")) for step in range(1, 10)]
        script.append(dict(text="All nine checks completed."))
        observed = []
        result = run("Observe nine items, then report", MockProvider(script),
            modes.ContextModeTests.registry(self, observed), lambda _: dict(ok=True), executor_only=True)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["turns"], 10)
        self.assertEqual(observed, list(range(1, 10)))

    def test_native_l0_final_reply_ends_without_forgiving_failed_acceptance(self):
        result, calls, _ = self.execute(self.response(), lambda: dict(ok=False, reason='real assertion failed'))
        self.assertEqual(result['status'], 'success', result.get('why'))
        self.assertFalse(result['verify']['ok'])
        self.assertEqual(calls, 1, 'no extra ready protocol or automatic model judge')

    def test_native_l0_truncated_response_is_not_normal_completion(self):
        result, calls, _ = self.execute(self.response('length'))
        self.assertEqual(result['status'], 'budget', result)
        self.assertEqual(calls, 1)

    def test_step_boundary_stop_arriving_with_final_reply_is_persisted(self):
        _,root,session,provider,_=self.owner('l0',calls=0)
        stopped=[False]
        def reply(*args,**kwargs):
            stopped[0]=True
            return self.response()
        with patch.object(provider,'_once',side_effect=reply),patch('loop.code_service.executor_verification',return_value=None):
            result=run('Wait at a safe step boundary',provider,Registry(),lambda:dict(ok=True),
                session=session,executor_only=True,max_turns=2,should_stop=lambda:stopped[0],should_stop_step=lambda:False)
        self.assertEqual(result['status'],'paused')
        self.assertEqual(session.load()['status'],'paused')

    def test_native_l0_unlimited_calls_has_no_hidden_three_read_stop(self):
        _,root,session,provider,_=self.owner('l0',calls=0)
        session.task_input=dict(id='task',revision=1,kind='new')
        core=EC.ExecutorCore(session,root)
        effects=[];registry=modes.ContextModeTests.registry(self,effects)
        n=[0]
        def reply(*args,**kwargs):
            n[0]+=1
            if n[0]>4:return self.response()
            return dict(choices=[dict(message=dict(tool_calls=[dict(id='read-'+str(n[0]),type='function',
                function=dict(name='observe',arguments=json.dumps(dict(step=1,material='x'))))]),finish_reason='tool_calls')])
        with patch.object(provider,'_once',side_effect=reply),patch('loop.code_service.executor_verification',return_value=None):
            result=run('Read four times, then report',provider,registry,lambda:dict(ok=True),session=session,
                executor_only=True,execution_core=core,max_turns=8)
        self.assertEqual(result['status'],'success',result.get('why'))
        self.assertEqual(effects,[1,1,1,1])

    def test_runtime_completion_keeps_current_required_evidence_missing(self):
        ident, root, session, provider, _ = self.owner('l0', calls=0)
        session.task_input = dict(id='task', revision=1, kind='new')
        core = EC.ExecutorCore(session, root)
        missing = dict(ok=False, requirements=[dict(id='required', text='Keep numeric result', status='unverified')])
        with patch.object(provider, '_once', return_value=self.response()), \
                patch('loop.code_service.executor_verification', return_value=None), \
                patch('loop.delivery_review.executor_gate', return_value=missing):
            result = run('Keep numeric result', provider, Registry(), lambda: dict(ok=True),
                session=session, executor_only=True, execution_core=core, max_turns=2)
        self.assertEqual(result['status'], 'success')
        self.assertFalse(result['verify']['ok'])
        completion = session.load()['executorCompletion']['completion']
        self.assertTrue(any(p['id']=='required' for p in completion['pending']))
        self.assertIn('未验收', completion['label'])

    def test_final_text_uses_same_pending_projection_as_completion_card(self):
        for state in ['unverified','failed']:
            with self.subTest(state=state):
                _,root,session,provider,_=self.owner('l0',calls=0)
                session.task_input=dict(id='task',revision=1,kind='new')
                core=EC.ExecutorCore(session,root)
                summary=dict(status='incomplete',resources=[dict(resourceId='code',status=state,checks=[],pending=[])])
                response=dict(choices=[dict(message=dict(content='Finished work.'),finish_reason='stop')])
                with patch.object(provider,'_once',return_value=response),patch('loop.code_service.executor_verification',return_value=summary):
                    run('Keep true failures visible',provider,Registry(),lambda:dict(ok=True),session=session,
                        executor_only=True,execution_core=core,max_turns=2)
                saved=session.load();pending=saved['executorCompletion']['completion']['pending']
                self.assertEqual(bool(pending),state=='failed')
                self.assertNotIn('当前版本尚未验收',saved['final'])
                self.assertNotIn('当前交付版本仍有未通过',saved['final'])

    def test_unavailable_final_projection_retains_failure_without_interrupting_shutdown(self):
        _,root,session,provider,_=self.owner('l0',calls=0)
        session.task_input=dict(id='task',revision=1,kind='new')
        core=EC.ExecutorCore(session,root)
        summary=dict(status='incomplete',resources=[dict(resourceId='code',status='failed',checks=[],pending=[])])
        with patch.object(provider,'_once',return_value=self.response()),patch('loop.code_service.executor_verification',return_value=summary),patch.object(core,'completion',side_effect=OSError('scope view temporarily unavailable')):
            result=run('Report actual failure',provider,Registry(),lambda:dict(ok=True),session=session,
                executor_only=True,execution_core=core,max_turns=2)
        self.assertEqual(result['status'],'success')
        saved=session.load()
        self.assertEqual(saved['verify']['codeChecks'],summary)
        self.assertIn('a check failed',saved['final'])
        self.assertNotIn('仍有未通过',saved['final'])

    def test_l0_summary_survives_retained_history_and_restart_without_helpers(self):
        _, root, session, provider, _ = self.owner('l0', calls=0)
        # DSH headroom is a total-window policy, independent of this fixture's
        # small input envelope. The previous 50K value conflated the two.
        CM.bind_capacity(session,provider,1048576)
        session.context_user_limit=50000
        session.task_input = dict(id='task', revision=1, kind='new')
        core=EC.ExecutorCore(session,root)
        registry=Registry();core.install(registry)
        registry.get('record_working_summary').fn(summary='Confirmed: return numeric Decimal, never a formatted string; regression reason: avoid float rounding.', sources=['calc.py:2'])
        effects=[]; registry.add(modes.ContextModeTests.registry(self,effects).get('observe'))
        seen,once=modes.ContextModeTests.replay(self,3)
        with patch.object(provider,'_once',side_effect=once),patch('loop.code_service.executor_verification',return_value=None):
            result=run('Preserve numeric return; work within this task',E.provider(provider,session,'executor'),registry,lambda:dict(ok=True),
                session=session,executor_only=True,execution_core=core,max_turns=24,max_same=100)
        self.assertEqual(result['status'],'success',result.get('why'))
        checkpoints=CM.checkpoint_page(session.dir)
        self.assertEqual(checkpoints['totalCheckpoints'],0,'below DSH pressure no history is retired')
        self.assertTrue(all('never a formatted string' in json.dumps(body) for body in seen))
        self.assertEqual(sum(m['role']=='tool' for m in seen[-1]['messages']),3)
        for record in checkpoints['checkpoints']:
            cp=json.loads((session.dir/'context-checkpoints'/('cp-'+record['id']+'.json')).read_text(encoding='utf8'))
            self.assertIn('never a formatted string',json.dumps(cp['candidate']))
        registry2=Registry();EC.ExecutorCore(session,root).install(registry2)
        self.assertIsNotNone(registry2.get('record_working_summary'))
        self.assertIsNone(registry2.get('request_scout'))
        self.assertIsNone(registry2.get('publish_review_candidate'))
        self.assertFalse((session.dir/'role-runs').exists())


class L0WorkTests(ConversationFixture, unittest.TestCase):
    owner=modes.ContextModeTests.owner

    def setUp(self):
        super().setUp()
        self.ident,self.root,self.session,_,_=self.owner('l0',calls=0)
        self.session.task_input=dict(id='task',revision=1,kind='new')
        self.core=EC.ExecutorCore(self.session,self.root)

    def observe(self,action,status,revision='v1',errors=None):
        job=dict(i=0,name='run_project_check',action_id=action,args=dict(resource_id='code',argv=['python','tests.py']))
        self.core.observe([job],{0:(dict(verification=dict(status=status,revision=revision),diagnostics=errors or {}),0)})

    def record_problem(self):
        with patch.object(EC.C,'context_resource'):
            return self.core.troubleshoot('code','actual regression','incorrect behavior',
                'confirmed using original input','investigating')['issue_id']

    def test_explicit_continuation_after_normal_end_keeps_task_and_new_instructions(self):
        checkpoint=dict(status='success',run_id='old',taskInput=dict(id='task',revision=1))
        kind=task_input.classify('继续：核对新增材料，保留原数值类型要求',checkpoint)
        self.assertEqual(kind,'amend')
        binding=task_input.bind(checkpoint['taskInput'],kind,'new',checkpoint)
        self.assertEqual(binding['id'],'task')
        self.assertEqual(binding['revision'],2)
        self.assertEqual(task_input.classify('继续',checkpoint),'resume')

    def test_explicit_new_task_and_pending_answer_still_take_precedence(self):
        checkpoint=dict(status='needs_human',run_id='old',waitQuestion=True)
        self.assertEqual(task_input.classify('新任务：继续阅读另一个项目',checkpoint),'new')
        self.assertEqual(task_input.classify('12\n补充：保留注释',checkpoint),'answer_amend')

    def test_error_header_and_exception_are_recorded_without_problem_jobs(self):
        self.observe('failed','failed',errors=dict(keyErrors=[dict(text='ERROR: test_x'),dict(text='AttributeError: x')]))
        self.observe('retry','failed',errors=dict(keyErrors=[dict(text='different traceback line')]))
        self.assertEqual(len(self.core.state['issues']),0)
        self.assertEqual(len(self.core.state['checks']),2)
        self.assertFalse((self.session.dir/'role-runs').exists())

    def test_pass_before_later_batched_write_cannot_close_issue(self):
        self.observe('failed','failed')
        self.record_problem()
        jobs=[dict(i=0,name='run_project_check',action_id='pass',args=dict(resource_id='code',argv=['python','tests.py'])),
              dict(i=1,name='edit_project_file',action_id='edit',args=dict(resource_id='code'))]
        self.core.observe(jobs,{0:(dict(verification=dict(status='passed',revision='v1')),0),1:(dict(ok=True,projectRevision='v2'),0)})
        self.assertNotEqual(next(iter(self.core.state['issues'].values()))['state'],'closed')

    def test_same_task_survives_restart_new_task_does_not_inherit_old_issues(self):
        self.observe('failed','failed')
        self.record_problem()
        self.assertEqual(len(EC.ExecutorCore(self.session,self.root).state['issues']),1)
        self.session.task_input=dict(id='new-task',revision=1,kind='new')
        self.assertEqual(EC.ExecutorCore(self.session,self.root).state['issues'],{})

    def test_resolved_claim_without_valid_receipt_stays_unverified(self):
        self.observe('failed','failed')
        key=self.record_problem()
        with patch.object(EC.C,'context_resource'):
            result=self.core.troubleshoot('code','problem','fixed implementation','Looks fixed','resolved',issue_id=key)
        self.assertEqual(result['issue']['state'],'awaiting_verification')

    def test_light_edit_invalidates_old_proof_even_without_project_scan(self):
        self.observe('failed','failed')
        key=self.record_problem()
        self.observe('passed','passed')
        self.core.observe([dict(i=0,name='edit_project_file',action_id='edit',args=dict(resource_id='code'))],
            {0:(dict(ok=True,projectRevision=None,fileChangeRevision='pending:edited'),0)})
        with patch.object(EC.C,'context_resource'):
            result=self.core.troubleshoot('code','problem','changed code','Old check passed','resolved',
                issue_id=key,evidence_action_id='passed')
        self.assertEqual(result['issue']['state'],'awaiting_verification')

    def test_changed_work_record_survives_later_runtime_update_compaction(self):
        body=dict(model='fixture',messages=[dict(role='system',content='Fixed policy'),
            dict(role='user',content='Original requirement'),
            dict(role='user',content='[context-update:executor-work:1]\nOriginal failure and attempted fix'),
            dict(role='user',content='[context-update:check-log:2]\nOther runtime data')])
        compact,_=CM.indexed_candidate(body,1,'checkpoint',runtime_indices=[2,3])
        self.assertIn('Original failure and attempted fix',json.dumps(compact))


if __name__ == '__main__':
    unittest.main()
