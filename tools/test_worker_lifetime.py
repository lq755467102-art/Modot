"""Durable worker handoff cases, including both sides of the wake claim."""
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from loop import execution_policy as E, permissions, subtasks as S, worker_plugins as P, workbench as W
from tools import test_worker_plugins as fixtures


class WorkerLifetimeTests(fixtures.ConversationFixture, TestCase):
    setup_worker=fixtures.WorkerContractTests.setup_worker
    parent_call=fixtures.WorkerContractTests.parent_call
    delegate=fixtures.WorkerContractTests.delegate
    running=fixtures.WorkerContractTests.running
    publish=fixtures.WorkerContractTests.publish
    manage=fixtures.WorkerContractTests.manage

    def configure(self,mode='wake',limit=0):
        self.setup_worker()
        E.configure(self.parent,dict(expectedRevision=1,workerContinuation=mode,modelCalls=limit,contextLimit=131072))
        self.config=E.saved_config(W.registry()['threads'][self.parent])
        W.atomic_json(self.budget,dict(W.read(self.budget),config=self.config))
        self.session=SimpleNamespace(dir=self.d,run_id=self.run,worker_contract=True,task_input=dict(id=self.task,revision=2))

    def complete(self):
        row=self.running(self.delegate())
        result=self.publish(row)
        S.finish_task(self.parent,row['id'],'completed','success')
        W.atomic_json(self.d/'session.json',dict(run_id=self.run,status='success',taskInput=self.session.task_input,msgs=[{'role':'user','content':'goal'}]))
        P.parent_finished(self.session,'success')
        W.atomic_json(self.d/'runtime.json',dict(W.RT.read(self.d),pid=0,identity=None,state='completed'))
        return row,result

    def test_claim_is_not_replayed_after_crash_before_launch(self):
        self.configure();row,result=self.complete()
        config=P.claim_wake(self.d,row['id'])
        self.assertEqual(config['workerContinuation'],'manual')
        with self.assertRaises(W.Problem):P.claim_wake(self.d,row['id'])
        self.session.run_id='manual-recovery'
        msgs=[];P.updates(self.session,msgs)
        self.assertIn(result['result_id'],msgs[0].content)
        P.included(self.session,{'messages':[m.dump() for m in msgs]},'actual')
        later=[];P.updates(self.session,later);self.assertEqual(later,[])
        self.assertEqual(len(S.listing(self.parent)),1)

    def test_pending_input_and_permission_change_preserve_manual_result(self):
        self.configure();row,_=self.complete()
        pending=self.d/'pending-after.json'
        W.atomic_json(pending,dict(state='queued'))
        with self.assertRaises(W.Problem):P.claim_wake(self.d,row['id'])
        pending.unlink()
        with patch.object(permissions,'profile',return_value={'revision':999}):
            with self.assertRaises(W.Problem):P.claim_wake(self.d,row['id'])
        self.assertNotIn('wakeClaim',S.load(self.parent,row['id'])['worker'])
        self.assertEqual(len(S.load(self.parent,row['id'])['worker']['results']),1)

    def test_finite_quota_keeps_original_allocation(self):
        self.configure(limit=4)
        row=self.delegate(model_calls=2)
        self.assertEqual(P.public(row)['lifetime'],'manual')
        self.assertEqual(S.execution_config(row)['modelCalls'],2)
        self.assertEqual(S.execution_config(row)['workerContinuation'],'parent-run')

    def test_human_dispatch_already_claimed_has_launch_priority(self):
        self.configure();row,_=self.complete()
        W.atomic_json(self.d/'pending-after.claimed.json',dict(state='waiting',request={'text':'补充需求'}))
        with self.assertRaises(W.Problem):P.claim_wake(self.d,row['id'])
        self.assertNotIn('wakeClaim',S.load(self.parent,row['id'])['worker'])
        W.atomic_json(self.d/'pending-after.result.json',dict(ok=True))
        self.assertEqual(P.claim_wake(self.d,row['id'])['workerContinuation'],'manual')

    def test_cross_run_idempotence_does_not_allocate_a_second_child(self):
        self.configure('manual');row=self.delegate()
        self.run='b'*32
        W.atomic_json(self.d/'runtime.json',dict(W.RT.read(self.d),run_id=self.run))
        ledger=self.d/'execution-runs'/(self.run+'.json')
        W.atomic_json(ledger,dict(runId=self.run,config=self.config,usedCalls=0,byRole={}))
        self.assertEqual(self.delegate()['id'],row['id'])
        with self.assertRaises(W.Problem) as changed:self.delegate(input={'note':'different'})
        self.assertEqual(changed.exception.code,'request_changed')
        self.assertEqual(len(S.listing(self.parent)),1)

    def test_new_parent_deadline_does_not_extend_existing_delegation(self):
        self.configure('manual')
        with patch('loop.control.benchmark_deadline',return_value=0):
            row=self.delegate()
        with patch('loop.control.benchmark_deadline',return_value=None):
            with self.assertRaises(W.Problem) as stopped:S._source(self.parent,row)
        self.assertEqual(stopped.exception.code,'worker_deadline')

    def test_old_version_result_retains_provenance_and_has_no_adoption(self):
        self.configure('manual')
        row=self.running(self.delegate(expected_revision='observed-version'))
        self.publish(row)
        self.session.run_id='another-run';self.session.task_input=dict(id=self.task,revision=3)
        msgs=[];P.updates(self.session,msgs)
        self.assertIn('observed-version',msgs[0].content)
        saved=S.load(self.parent,row['id'])['worker']['results'][0]
        self.assertEqual(saved['source_scope']['demand_revision'],2)
        self.assertEqual(saved['source_scope']['source_mode'],'live-read')
        self.assertIsNone(saved['disposition'])

    def test_overview_distinguishes_notice_from_body_delivery(self):
        self.configure('manual');row=self.running(self.delegate());saved=self.publish(row)
        def shown():
            return S._overview(S.load(self.parent,row['id']))['resultDelivery']
        self.assertEqual(shown(),'available')
        msgs=[];P.updates(self.session,msgs)
        P.included(self.session,{'messages':[m.dump() for m in msgs]},'notice')
        self.assertEqual(shown(),'notified')
        import json
        body=self.manage(row,'result',result_id=saved['result_id'])
        P.included(self.session,{'messages':[dict(role='tool',content=json.dumps(body))]},'body')
        self.assertEqual(shown(),'delivered')
