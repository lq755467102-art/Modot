"""Concrete public contracts use existing notes, not automatic acceptance."""
import json
import unittest
from tools.test_conversations import ConversationFixture
from tools import test_agent_code as fixtures
from loop import working_context as N
from loop.executor_core import ExecutorCore
from loop.tools import default_registry


class ContractNotes(ConversationFixture,unittest.TestCase):
    setup_code=fixtures.AgentCodeTests.setup_code
    grant=fixtures.AgentCodeTests.grant

    def setUp(self):
        super().setUp();self.setup_code()
        self.session.task_input=dict(id='contracts-task',revision=1)
        self.session.context_scope=dict(resource=self.r['id'])
        self.core=ExecutorCore(self.session,self.execution_root)
        self.reg=default_registry(self.execution_root);self.core.install(self.reg)

    def test_concrete_inputs_and_compatibility_reach_resume_without_becoming_a_pass(self):
        contract=dict(input='public alias "overview"',expected='output summary.html',
                      preserve='existing fields-only path retains its legacy error identity',check='tests/test_public_entry.py')
        record=self.reg.get('record_working_summary').fn
        self.core.state['versions']={self.r['id']:'confirmed-version'}
        record(summary='Confirmed public entry differs from generated output.',sources=['api.py:12'],contracts=[contract])
        self.core.state['versions'][self.r['id']]='later-version'
        # An ordinary progress update must not accidentally erase known contracts.
        record(summary='Implementation changed; verification still missing.',sources=['action:edit'])
        messages=[];N.append_updates(self.session,'executor',messages)
        saved=json.loads(N.summary_path(self.session,'executor').read_text(encoding='utf8'))
        self.assertEqual(saved['contracts'],[contract]);self.assertEqual(saved['contractSources'],['api.py:12'])
        self.assertEqual(saved['contractBasis']['versions'][self.r['id']],'confirmed-version')
        self.assertEqual(saved['contractBasis']['demandRevision'],1)
        self.assertIn('summary.html',messages[-1].content)
        self.assertEqual(self.core.read()['checks'],{})
        self.assertEqual(self.core.read()['pending'],[])

    def test_new_task_and_changed_scope_do_not_inherit_contracts(self):
        record=self.reg.get('record_working_summary').fn
        record(summary='First task',sources=['api.py'],contracts=[dict(input='old',expected='old output')])
        self.session.task_input=dict(id='different-task',revision=1)
        record(summary='Independent task',sources=['new request'])
        row=json.loads(N.summary_path(self.session,'executor').read_text())
        self.assertNotIn('contracts',row)
        record(summary='Other task',sources=['new API'],contracts=[dict(input='new',expected='new output')])
        self.session.context_scope=dict(resource='different-root')
        messages=[];N.append_updates(self.session,'executor',messages)
        self.assertFalse(any('working-summary' in m.content for m in messages))

    def test_snapshot_timestamp_refresh_keeps_contracts_but_revoke_does_not(self):
        self.session.context_scope=dict(at=1,projectId='same',resources=[dict(id=self.r['id'],write=True)])
        record=self.reg.get('record_working_summary').fn
        record(summary='Confirmed',sources=['api:1'],contracts=[dict(input='alias',expected='public output')])
        self.session.context_scope['at']=2
        result=record(summary='Continue actual verification',sources=['progress'])
        self.assertEqual(result['contracts'][0]['input'],'alias')
        self.session.context_scope['resources'][0]['write']=False
        messages=[];N.append_updates(self.session,'executor',messages)
        self.assertFalse(any('working-summary' in m.content for m in messages))

    def test_scoped_roles_can_save_notes_with_the_original_limit(self):
        for role in ('planner', 'reviewer'):
            with self.subTest(role=role):
                reg = default_registry(self.execution_root)
                N.install(self.session, role, reg)
                tool = reg.get('record_working_summary')
                self.assertNotIn('contracts', tool.parameters['properties'])
                self.assertTrue(tool.fn(summary='x' * 6000, sources=['original limit'])['ok'])

    def test_scout_is_not_an_independent_note_owner(self):
        from loop.workbench import Problem
        reg = default_registry(self.execution_root)
        N.install(self.session, 'scout', reg)
        with self.assertRaises(Problem):
            reg.get('record_working_summary').fn(summary='role note', sources=['method'])

    def test_explicit_corrected_contract_replaces_old_without_a_gate(self):
        record=self.reg.get('record_working_summary').fn
        record(summary='Confirmed',sources=['api:1'],contracts=[dict(input='old',expected='old')])
        result=record(summary='Original assumption corrected from caller evidence.',sources=['caller:8'],
                      contracts=[dict(input='public',expected='mapped')])
        self.assertEqual(result['contracts'],[dict(input='public',expected='mapped')])
        self.assertEqual(result['contractSources'],['caller:8'])

    def test_contract_payload_survives_real_compaction_projection(self):
        from loop import context_manager as CM
        contract=dict(input='overview',expected='summary.html',preserve='legacy input',check='test_public_entry')
        self.reg.get('record_working_summary').fn(summary='Confirmed API mapping',sources=['api.py:1'],contracts=[contract])
        messages=[];N.append_updates(self.session,'executor',messages)
        note=messages[-1].dump()
        body=dict(messages=[dict(role='system',content='No new permissions'),dict(role='user',content='Original requirement'),note])
        for n in range(8):
            body['messages'].extend([dict(role='assistant',content='',tool_calls=[dict(id=str(n),type='function',function=dict(name='inspect_code',arguments='{}'))]),
                dict(role='tool',tool_call_id=str(n),content='read material '*500)])
        compact,_=CM.candidate(body,keep_tools=1,checkpoint_id='contract-projection')
        text=json.dumps(compact,ensure_ascii=False)
        for value in contract.values():self.assertIn(value,text)
        self.assertEqual(compact['messages'][0]['content'],'No new permissions')
        CM.validate_pairs(compact['messages'])

    def test_prior_observations_are_labeled_against_new_demand_without_rewriting_history(self):
        record=self.reg.get('record_working_summary').fn
        contract=dict(input='overview',expected='summary.html',check='api.py:1')
        record(summary='Source-backed first slice; new requests remain independent.',sources=['api.py:1'],contracts=[contract])
        messages=[];N.append_updates(self.session,'executor',messages)
        before=[m.dump() for m in messages]
        disk=N.summary_path(self.session,'executor').read_bytes()
        self.session.task_input['revision']=2
        N.append_updates(self.session,'executor',messages)
        self.assertEqual([m.dump() for m in messages[:len(before)]],before)
        view=json.loads(messages[-1].content.split('\n',2)[2])
        self.assertEqual(messages[-1].role,'assistant')
        self.assertNotIn('contracts',view)
        for value in contract.values():self.assertIn(value,view['summary'])
        self.assertIn('第1轮',view['contractContext']);self.assertIn('第2轮',view['contractContext'])
        self.assertIn('未覆盖本轮新增要求',view['contractContext'])
        self.assertEqual(N.summary_path(self.session,'executor').read_bytes(),disk)
        length=len(messages);N.append_updates(self.session,'executor',messages)
        self.assertEqual(len(messages),length,'do not resend the same observation on every tool result')
        self.assertEqual(self.core.read()['checks'],{})

    def test_explicit_new_observation_has_current_basis_without_forgiving_old_checks(self):
        record=self.reg.get('record_working_summary').fn
        record(summary='Prior result',sources=['old.py'],contracts=[dict(input='alias',expected='old output')])
        self.session.task_input['revision']=2
        record(summary='New input confirmed from current caller.',sources=['caller.py:4'],
            contracts=[dict(input='alias',expected='new output')])
        messages=[];N.append_updates(self.session,'executor',messages)
        view=json.loads(messages[-1].content.split('\n',2)[2])
        self.assertEqual(view['contractBasis']['demandRevision'],2)
        self.assertNotIn('旧观察',view['contractContext'])
        self.assertIn('不代表当前版本验收',view['contractContext'])

    def test_missing_legacy_basis_is_unknown_not_implicitly_current(self):
        self.reg.get('record_working_summary').fn(summary='Legacy',sources=['old.py'],
            contracts=[dict(input='alias',expected='output')])
        path=N.summary_path(self.session,'executor');row=json.loads(path.read_text(encoding='utf8'))
        row.pop('contractBasis');path.write_text(json.dumps(row),encoding='utf8')
        messages=[];N.append_updates(self.session,'executor',messages)
        view=json.loads(messages[-1].content.split('\n',2)[2])
        self.assertIn('来源轮次未记录',view['contractContext'])
        self.assertNotIn('contractBasis',view)

    def test_old_observation_and_new_requirement_survive_compaction_with_recent_failure(self):
        from loop import context_manager as CM
        self.reg.get('record_working_summary').fn(summary='First public entry observed',sources=['api.py:1'],
            contracts=[dict(input='overview',expected='summary.html')])
        self.session.task_input['revision']=2
        messages=[];N.append_updates(self.session,'executor',messages)
        body=dict(messages=[dict(role='system',content='Preserve real failures'),
            dict(role='user',content='NEW_REQUIREMENT: quickview must work too'),messages[-1].dump()])
        for i in range(9):
            body['messages'].extend([dict(role='assistant',content='',tool_calls=[dict(id=str(i),type='function',
                function=dict(name='run_project_check',arguments='{}'))]),
                dict(role='tool',tool_call_id=str(i),content='ERROR: real failure remains '+str(i))])
        compact,_=CM.candidate(body,keep_tools=2,checkpoint_id='old-observation')
        text=json.dumps(compact,ensure_ascii=False)
        for value in ['第1轮','第2轮','未覆盖本轮新增要求','NEW_REQUIREMENT','ERROR: real failure remains 8']:
            self.assertIn(value,text)
        CM.validate_pairs(compact['messages'])


if __name__=='__main__':unittest.main(verbosity=2)
