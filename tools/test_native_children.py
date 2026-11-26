"""Native L0 parity through managed child launch, tools, history and limits."""
import json
import os
import importlib.util
import io
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch
import unittest
from types import SimpleNamespace

from loop import execution_policy as E, parallel_budget as B, subtasks as S, workbench as W
from loop.native_entry import NativeEntry, selected
from loop.provider import Msg, MockProvider
from loop.session import Session
from loop.tools import default_registry, set_call, reset_call
from tools.test_conversations import ConversationFixture
from tools.test_native_entry import guard_legacy_imports


class NativeChildTests(ConversationFixture, unittest.TestCase):
    def child(self, *, calls=None, parent_calls=0, max_turns=0):
        _, parent = self.call('POST')
        directory = self.sv._resolve_sid(parent['id'])
        run = W.RT.register(directory, os.getpid())['run_id']
        config = dict(E.defaults(), modelCalls=parent_calls, maxTurns=max_turns, contextLimit=131072)
        target = directory/'execution-runs'/(run+'.json')
        W.atomic_json(target, dict(runId=run, config=config, usedCalls=0, byRole={}))
        from loop import permissions
        permissions.freeze(run, parent['id'])
        token = set_call(dict(sessionId=parent['id'], runId=run, actionId='a'*32, tool='create_subtask'))
        try:
            args = dict(request_key='native', goal='Remember CHILD_ONLY; investigate independently.', resource_ids=[], kind='report')
            if calls is not None:
                args['model_calls'] = calls
            row = S.create(parent['id'], args, self.sv, schedule=False)
        finally:
            reset_call(token)
        S._prepare(row, self.sv)
        row = S.load(parent['id'], row['id'])
        row['state'] = 'running'
        S.save(row)
        return row, target

    def test_child_is_native_and_inherits_real_configuration(self):
        row, target = self.child(max_turns=17)
        config = S.execution_config(row)
        root = W.workspace(W.registry()['threads'][row['childId']]['workspaceId'])['path']
        self.assertTrue(selected(config))
        self.assertEqual((config['contextLimit'], config['maxTurns'], config['modelCalls']), (131072, 17, 0))
        self.assertEqual(E.runner_config(config, root), config)
        for key, value in [('contextLimit',262144), ('maxTurns',8), ('modelCalls',4)]:
            with self.subTest(key=key), self.assertRaises(W.Problem):
                E.runner_config(dict(config, **{key:value}), root)
        for _ in range(45):
            B.consume(target, 'executor', row['id'])
        self.assertEqual(B._read(target)['usedCalls'],45)

    def test_explicit_budget_is_respected_without_old_forty_call_cap(self):
        row, target = self.child(calls=50, parent_calls=60)
        self.assertEqual(S.execution_config(row)['modelCalls'],50)
        for _ in range(50):
            B.consume(target,'executor',row['id'])
        with self.assertRaises(E.BudgetExhausted):
            B.consume(target,'executor',row['id'])
        with self.assertRaises(W.Problem):
            B.reserve(target,'bypass',0)

    def test_omitted_budget_uses_remaining_finite_allocation(self):
        row,target=self.child(parent_calls=7)
        self.assertEqual(row['budget']['limit'],7)
        with self.assertRaises(E.BudgetExhausted):
            B.consume(target,'executor')
        B.consume(target,'executor',row['id'])
        B.release(target,row['id'],confirmed=True)
        self.assertEqual(B.consume(target,'executor')['usedCalls'],2)

    def test_parent_collects_finished_children_once_while_sibling_is_busy(self):
        row, _ = self.child()
        parent = SimpleNamespace(dir=self.sv._resolve_sid(row['parentId']),
                                 run_id=row['parentRunId'], emit=Mock())
        sibling = dict(row, id='b'*32, childId='sibling')
        S.save(sibling)
        self.assertIsNone(S.join(parent, wait=False, timeout=0))
        row.update(state='completed', result={'final':'first finding'})
        S.save(row)
        self.assertEqual([r['id'] for r in S.join(parent, wait=False, timeout=0)['tasks']], [row['id']])
        self.assertIsNone(S.join(parent, wait=False, timeout=0))
        sibling.update(state='failed', reason='independent check failed')
        S.save(sibling)
        result = S.join(parent)
        self.assertEqual([(r['id'], r['state']) for r in result['tasks']], [(sibling['id'], 'failed')])
        self.assertIsNone(S.join(parent))
        self.assertEqual(parent.emit.call_count, 2)

    def test_parent_waits_for_reserved_calls_only_when_its_own_budget_is_unavailable(self):
        row, target = self.child(parent_calls=3)
        parent = SimpleNamespace(dir=self.sv._resolve_sid(row['parentId']),
                                 run_id=row['parentRunId'], emit=Mock())
        def finish(_seconds):
            B.consume(target, 'executor', row['id'])
            S.finish_task(row['parentId'], row['id'], 'completed', 'done', {'final':'found'})
        with patch.object(S.time, 'sleep', side_effect=finish) as waited:
            self.assertEqual(S.join(parent, wait=False)['tasks'][0]['result']['final'], 'found')
        waited.assert_called_once()
        self.assertEqual(B.consume(target, 'executor')['usedCalls'], 2)

    def test_native_recovery_keeps_unknown_reserved_until_matching_exit_and_result(self):
        row, target = self.child(calls=2, parent_calls=5)
        directory = self.sv._resolve_sid(row['childId'])
        row['childRunId'] = 'child-run'
        S.save(row)
        W.atomic_json(directory/'runtime.json', {'run_id':'child-run','pid':0,'state':'running'})
        W.atomic_json(directory/'session.json', {'run_id':'old-run','status':'success','final':'stale finding'})
        with patch.object(S, 'schedule') as schedule:
            S.recover(self.sv)
            self.assertEqual(S.load(row['parentId'],row['id'])['state'], 'unknown')
            self.assertFalse(B._read(target)['allocations'][row['id']]['terminal'])
            W.atomic_json(directory/'session.json', {'run_id':'child-run','status':'failed','final':'real failure'})
            S.recover(self.sv)
            S.recover(self.sv)
        schedule.assert_not_called()
        saved = S.load(row['parentId'],row['id'])
        self.assertEqual((saved['state'],saved['result']['final']), ('failed','real failure'))
        self.assertTrue(B._read(target)['allocations'][row['id']]['terminal'])

    def test_explicit_cancel_can_close_an_unowned_native_child_after_confirmed_exit(self):
        from loop import control
        row, target = self.child(calls=2, parent_calls=5)
        directory = self.sv._resolve_sid(row['childId'])
        row['childRunId'] = 'native-attempt'
        S.save(row)
        W.atomic_json(directory/'runtime.json', {'run_id':'another-attempt', 'pid':0, 'state':'running'})
        S.recover(self.sv)
        self.assertEqual(S.load(row['parentId'],row['id'])['state'], 'unknown')
        self.assertEqual(S.cancel(row['parentId'],row['id'],self.sv)['state'], 'unknown')
        self.assertFalse(control.path(directory).exists())
        self.assertFalse(B._read(target)['allocations'][row['id']]['terminal'])
        W.atomic_json(directory/'runtime.json', {'run_id':'native-attempt', 'pid':0, 'state':'running'})
        with patch.object(W.RT, 'status', return_value={'run_id':'native-attempt','active':True,'state':'unknown'}):
            self.assertEqual(S.cancel(row['parentId'],row['id'],self.sv)['state'], 'unknown')
            self.assertFalse(B._read(target)['allocations'][row['id']]['terminal'])
            self.assertTrue(control.path(directory).exists())
        S.recover(self.sv)
        result = S.cancel(row['parentId'],row['id'],self.sv)
        self.assertEqual(result['state'], 'cancelled')
        self.assertTrue(B._read(target)['allocations'][row['id']]['terminal'])

    def test_child_tools_and_history_use_native_core_and_resume(self):
        row, _ = self.child()
        child = W.registry()['threads'][row['childId']]
        root = Path(W.workspace(child['workspaceId'])['path'])
        session = Session(self.sv._resolve_sid(row['childId']))
        session.run_id = W.RT.register(session.dir, os.getpid())['run_id']
        session.task_input = dict(id='child-logical', revision=1, kind='new')
        entry = NativeEntry(session, root, lambda:False, delegated=True)
        registry = default_registry(root)
        with patch('builtins.__import__', side_effect=guard_legacy_imports):
            system, fragments, task, parts = entry.prepare(registry, 'BASE','BASE',session.task_input,
                None, row['goal'], [('task',row['goal'])], None)
            self.assertIs(session.executor_core, entry.core)
            names = {schema['name'] for schema in registry.schemas()}
            self.assertTrue({'use_tool_group','search_project_code','read_task_requirements','ask_human'} <= names)
            self.assertTrue({'edit_project_file','run_project_check','record_working_summary'}.isdisjoint(names))
            token = set_call(dict(sessionId=child['id'],runId=session.run_id,actionId='b'*32,tool='use_tool_group'))
            try:
                registry.get('use_tool_group')(dict(group='verification'))
                self.assertIn('inspect_delivery', {schema['name'] for schema in registry.schemas()})
                for forbidden in ('create_subtask','prepare_subtask_integration','install_catalog_plugin'):
                    with self.subTest(tool=forbidden), self.assertRaises(W.Problem):
                        registry.get(forbidden)({})
            finally:
                reset_call(token)
            result = entry.run(row['goal'], MockProvider([dict(text='CHILD_ONLY_RESULT',finish_reason='stop')]),
                registry, lambda:dict(ok=False), session=session, system=system, system_fragments=fragments,
                task_message=Msg('user',task,context_parts=parts), ctx_limit=131072, max_turns=0)
        entry.close()
        self.assertEqual(result['status'],'success')
        saved = session.load()
        self.assertIn('CHILD_ONLY_RESULT',json.dumps(saved['msgs']))
        self.assertNotIn('CHILD_ONLY_RESULT',json.dumps(W.read(Path(row['rootBudget']).parent.parent/'session.json')))
        messages = [Msg.load(m) for m in saved['msgs']]
        session.task_input['kind'] = 'resume'
        resumed = NativeEntry(session,root,lambda:False,delegated=True)
        try:
            reg = default_registry(root)
            _, _, _, _ = resumed.prepare(reg,'BASE','BASE',session.task_input,messages,task,parts,None)
            self.assertIn('inspect_delivery',{s['name'] for s in reg.schemas()})
            self.assertIn('CHILD_ONLY_RESULT',json.dumps([m.dump() for m in messages]))
        finally:
            resumed.close()

    def test_managed_main_resumes_own_checkpoint_without_legacy_roles(self):
        row, target = self.child()
        child = W.registry()['threads'][row['childId']]
        root = Path(W.workspace(child['workspaceId'])['path'])
        directory = self.root/'runs'/child['id']
        directory.mkdir(parents=True,exist_ok=True)
        config = S.execution_config(row)
        task = self.root/'child-task.md'
        task.write_text(row['goal'],encoding='utf8')
        spec = importlib.util.spec_from_file_location('native_child_main',
            Path(__file__).resolve().parents[1]/'bin/run-task.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        provider = MockProvider([dict(tool='ask_human',args=dict(question='CHILD_QUESTION: which option?')),
                                 dict(text='CHILD_RESUMED',finish_reason='stop')])
        provider.base_url,provider.model,provider.api_key='','fixture',''
        previous = os.getcwd()
        requests=[]
        leases=[]
        original_lease=module.Lease
        def lease(*args,**kwargs):
            value=original_lease(*args,**kwargs)
            leases.append(value)
            return value
        original = provider.chat
        def capture(messages, tools, **kwargs):
            requests.append([m.dump() for m in messages])
            return original(messages,tools,**kwargs)
        try:
            with (patch('builtins.__import__',side_effect=guard_legacy_imports),
                  patch.object(module.PR,'chain_from_env',return_value=provider),
                  patch.object(module,'Lease',side_effect=lease),
                  patch.object(provider,'chat',side_effect=capture),
                  patch.dict(os.environ,HARNESS_MODELS=str(self.root/'no-models'),
                             HARNESS_CREDS=str(self.root/'no-credentials')),
                  redirect_stdout(io.StringIO())):
                module.main(['--task',str(task),'--session',str(directory),'--cwd',str(root),
                    '--out',str(directory/'out.md'),'--execution-config',json.dumps(config),'--no-stream'])
                self.assertEqual(W.read(directory/'session.json')['status'],'needs_human',
                                 W.read(directory/'session.json').get('why'))
                # main's CLI owner normally exits here; this test shares a process.
                leases[-1].release()
                W.atomic_json(directory/'runtime.json',dict(W.RT.read(directory),pid=0,identity=None,
                    state='waiting_approval',phase='finished',outcome='needs_human'))
                module.main(['--resume',str(directory),'--say','Use the existing evidence; continue.',
                    '--cwd',str(root),'--out',str(directory/'out.md'),
                    '--execution-config',json.dumps(config),'--no-stream'])
            state=W.read(directory/'session.json')
            self.assertEqual(state['status'],'success')
            self.assertEqual(state['final'],'CHILD_RESUMED')
            self.assertIn('CHILD_QUESTION',json.dumps(requests[-1]))
            self.assertIn('Use the existing evidence',json.dumps(requests[-1]))
            self.assertEqual(B._read(target)['usedCalls'],2)
            self.assertEqual(state['executorWork']['taskId'],state['taskInput']['id'])
        finally:
            for value in leases:
                value.release()
            os.chdir(previous)

    def test_native_guard_keeps_write_paths_and_rechecks_parent_lifetime(self):
        row,_ = self.child()
        row.update(workItemId='feature',writePaths=['allowed.py'])
        S.save(row)
        from loop.tools import Registry,Tool
        invoked=[]
        registry=Registry()
        for name,kind in [('edit_project_file','write'),('initialize_code_project','write'),
                          ('restore_project_file','write'),('run_project_check','exec')]:
            registry.add(Tool(name,'fixture',dict(type='object'),lambda **args:invoked.append(args),zone='offflow',kind=kind))
        S.adapt_registry(registry, row['childId'])
        token=set_call(dict(sessionId=row['childId'],runId='fixture',actionId='c'*32,tool='edit_project_file'))
        try:
            registry.get('edit_project_file')(dict(relative_path='allowed.py'))
            for name,args in [('edit_project_file',dict(relative_path='outside.py')),
                              ('initialize_code_project',{}),('restore_project_file',{}),
                              ('run_project_check',dict(capture_changes=True))]:
                with self.subTest(tool=name), self.assertRaises(W.Problem):
                    registry.get(name)(args)
            self.assertEqual(len(invoked),1)
            with patch.object(S,'_source',side_effect=W.Problem('parent_owner_lost','stopped',409)):
                with self.assertRaises(W.Problem):
                    registry.get('edit_project_file')(dict(relative_path='allowed.py'))
        finally:
            reset_call(token)


if __name__ == '__main__':
    unittest.main()
