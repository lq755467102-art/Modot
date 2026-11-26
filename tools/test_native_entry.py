"""Native/legacy entry separation at real entry and tool seams; no model network."""
import builtins
from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import itertools
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock

from loop import execution_policy as E
from loop.native_entry import NativeEntry, selected
from loop.provider import Interrupted, MockProvider, Msg
from loop.session import Session
from loop.tools import default_registry
from tools.test_l0_core_slimming import NativeToolsFixture, CORE, exposed


FORBIDDEN = {'loop.legacy_entry', 'loop.scoped_roles', 'loop.role_coordination',
             'loop.background_planning', 'loop.assistant_patches', 'loop.executor_workflow',
             'loop.delegations'}


def guard_legacy_imports(name, globals=None, locals=None, fromlist=(), level=0):
    module = importlib.util.resolve_name('.' * level + name, globals['__package__']) if level else name
    targets = {module, *(module + '.' + item for item in (fromlist or ()) if item != '*')}
    if targets & FORBIDDEN:
        raise AssertionError('native route imported a legacy role: ' + repr(targets & FORBIDDEN))
    return ORIGINAL_IMPORT(name, globals, locals, fromlist, level)


ORIGINAL_IMPORT = builtins.__import__


class NativeEntryTests(NativeToolsFixture):
    def setUp(self):
        super().setUp()
        self.session.task_input = dict(self.session.task_input, kind='new')
        self.entry = NativeEntry(self.session, self.root, lambda: False)
        self.addCleanup(self.entry.close)

    def prepare(self, messages=None, contract=None):
        self.reg = default_registry(self.root)
        return self.entry.prepare(self.reg, 'BASE', 'BASE', self.session.task_input,
                                  messages, 'Original requirement', [('task', 'Original requirement')], contract)

    def test_old_flags_and_recovery_never_select_legacy_for_native_l0(self):
        for parallel, planner in itertools.product((False, True), repeat=2):
            config = E.runner_config(dict(E.defaults(), contextLimit=131072,
                parallelRoles=parallel, backgroundPlanner=planner))
            self.assertTrue(selected(config))
            self.assertEqual(config['contextLimit'], 131072)
        self.assertTrue(selected(dict(E.defaults(), subtaskId='a'*32)))
        for config in (None, dict(E.defaults(), crew='l1'),
                       dict(E.defaults(), purpose='delegation_draft')):
            self.assertFalse(selected(config))

    def test_actual_native_install_and_final_reply_without_legacy_imports(self):
        provider = MockProvider([dict(text='Result and unverified scope.', finish_reason='stop')])
        with patch('builtins.__import__', side_effect=guard_legacy_imports):
            system, fragments, task, parts = self.prepare()
            self.assertEqual(exposed(self.reg), CORE)
            result = self.entry.run('Original requirement', provider, self.reg,
                lambda: dict(ok=False, why='not verified'), session=self.session,
                system=system, system_fragments=fragments, max_turns=0,
                ctx_limit=131072, task_message=Msg('user', task, context_parts=parts),
                should_stop=lambda: False)
        self.assertEqual(result['status'], 'success')
        self.assertEqual(result['final'], 'Result and unverified scope.')
        self.assertEqual(provider.i, 1)
        self.assertFalse(result.get('verify', {}).get('ok'))

    def test_managed_main_does_not_import_legacy_or_restore_its_wait(self):
        # Exercise bin/run-task itself, not just an entry adapter selected by a test.
        spec = importlib.util.spec_from_file_location('native_entry_main',
            Path(__file__).resolve().parents[1]/'bin/run-task.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        task = self.fixture.root/'native-task.md'
        task.write_text('Only provide a factual reply.', encoding='utf8')
        config = self.fixture.root/'native-execution.json'
        config.write_text(json.dumps(dict(E.defaults(), contextLimit=131072,
            parallelRoles=True, backgroundPlanner=True)), encoding='utf8')
        provider = MockProvider([dict(text='Fixed native completion.', finish_reason='stop')])
        provider.base_url, provider.model, provider.api_key = '', 'fixture', ''
        private_session = Session(self.fixture.root/'runs'/self.fixture.t['id'])
        private_session.save(status='paused', task=task.read_text(encoding='utf8'),
            msgs=[Msg('user', 'Only provide a factual reply.').dump()],
            roleWait=dict(role='reviewer', executorRun='old-executor', receipts=[], agreementVersion=None))
        import os
        previous = os.getcwd()
        try:
            with (patch('builtins.__import__', side_effect=guard_legacy_imports),
                  patch.object(module.PR, 'chain_from_env', return_value=provider),
                  patch.dict(os.environ, HARNESS_MODELS=str(self.fixture.root/'no-models'),
                             HARNESS_CREDS=str(self.fixture.root/'no-credentials')),
                  redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
                args = ['--resume', str(private_session.dir),
                    '--cwd', str(self.root), '--out', str(self.fixture.root/'answer.md'),
                    '--execution-config', config.read_text(encoding='utf8'), '--no-stream']
                with self.assertRaises(SystemExit) as removed:
                    module.main([*args, '--resume-role-run', 'a'*32])
                self.assertEqual(removed.exception.code, 2)
                self.assertEqual(provider.i, 0)
                result = module.main(args)
            self.assertEqual(result, 0)
            self.assertEqual(provider.i, 1)
            self.assertIsNone(private_session.load().get('roleWait'))
            events = [json.loads(line) for line in private_session.events_path.read_text('utf8').splitlines()]
            self.assertFalse(any(event.get('act') in {'role_recovery_request', 'benni'} for event in events))
        finally:
            os.chdir(previous)

    def test_resume_keeps_required_evidence_notice_with_original_id(self):
        self.session.task_input['kind'] = 'resume'
        messages = [Msg('user', 'Original requirement')]
        requirement = dict(id='required-original', text='Public entry remains compatible',
                           status='pending', needsRebind=True)
        with patch('loop.delivery_review.executor_gate', return_value=dict(requirements=[requirement])):
            self.prepare(messages)
        self.assertEqual(len(messages), 2)
        self.assertIn('required-original', messages[-1].content)
        self.assertIn('needsRebind', messages[-1].content)

    def test_unreadable_required_ledger_keeps_existing_refresh_instruction(self):
        self.session.task_input['kind'] = 'resume'
        with patch('loop.delivery_review.executor_gate', side_effect=OSError('unreadable')):
            system, fragments, _, _ = self.prepare([])
        self.assertIn('续接前请用 inspect_delivery', system)
        self.assertIn('续接前请用 inspect_delivery', fragments.render())

    def test_explicit_contract_text_survives_new_and_resumed_entry(self):
        folder = self.fixture.root/'explicit-contract'
        folder.mkdir()
        (folder/'done.json').write_text(json.dumps(dict(intent='User locked intent',
            planner_note='Observed setup issue')), encoding='utf8')
        _, _, task, parts = self.prepare(contract=folder)
        self.assertIn('User locked intent', task)
        self.assertEqual(parts[-1][0], 'agreement')
        messages = [Msg('user', 'Original requirement')]
        self.prepare(messages, folder)
        self.assertIn('User locked intent', messages[-1].content)
        self.assertNotIn('Observed setup issue', messages[-1].content)

    def test_boundary_stop_publishes_corrected_status_once(self):
        self.prepare()
        result = dict(status='paused', turns=3, why='boundary stop', verify=dict(ok=False))
        self.session.save(status='success')
        with patch.object(self.entry.core, 'finish', wraps=self.entry.core.finish) as finish:
            self.entry.settle(result)
            self.entry.settle(result)
        self.assertEqual(finish.call_count, 1)
        self.assertEqual(self.session.load()['status'], 'paused')

    def test_startup_interruption_preserves_checkpoint_without_role_dispatch(self):
        messages = [Msg('user', 'Original requirement')]
        result = self.entry.interrupted(Interrupted('user stopped'), messages, 'Original requirement')
        state = self.session.load()
        self.assertEqual(result['status'], 'paused')
        self.assertIsNone(state['roleWait'])
        events = [json.loads(line) for line in self.session.events_path.read_text('utf8').splitlines()]
        self.assertEqual(events[-1]['role'], 'executor')
        self.assertEqual(state['msgs'], [m.dump() for m in messages])
        self.assertEqual(self.entry.interrupt_errors, (Interrupted,))


class LegacyEntryTests(unittest.TestCase):
    def entry(self, **overrides):
        from loop.legacy_entry import LegacyEntry
        args = SimpleNamespace(no_benni=False, plan_first=False, resume=True,
                               say='', max_turns=0)
        values = dict(args=args, session=Mock(run_id='run', dir=Path('fixed-session')), root=Path('.'), task='Original task',
            task_input=dict(id='task', kind='resume'), config=dict(E.defaults(), crew='l4'),
            previous=dict(status='paused', roleWait=dict(role='reviewer', agreementVersion='locked-version',
                executorRun='previous-executor', receipts=[], plan='Retained plan'), final='Retained output'),
            input_kind='resume', controller={'contract':{'version':'locked-version'}}, bound=True, stop=lambda: False, watch=lambda: False, live=None)
        values.update(overrides)
        return LegacyEntry(**values)

    def test_delegation_reviewer_resume_keeps_executor_result_and_plan(self):
        entry = self.entry()
        self.assertTrue(entry.resume_review)
        result = entry.resume_result([Msg('assistant', 'Retained output')])
        self.assertEqual(result['final'], 'Retained output')
        self.assertEqual(entry.session.review_executor_run, 'previous-executor')
        entry.plan_text = entry.recovery['plan']
        entry.save_role_stop('reviewer', 'paused', 'wait')
        self.assertEqual(entry.session.save.call_args.kwargs['roleWait']['plan'], 'Retained plan')

    def test_delegation_amendment_does_not_resume_review(self):
        entry = self.entry(input_kind='amend')
        self.assertFalse(entry.resume_review)
        args = SimpleNamespace(no_benni=True, plan_first=False, resume=True,
                               say='', max_turns=0)
        self.assertFalse(self.entry(args=args).resume_review)

    def test_delegation_planner_direction_stays_before_executor_followup(self):
        args = SimpleNamespace(no_benni=True, plan_first=True, resume=True,
                               say='New amendment', max_turns=0)
        entry = self.entry(args=args, previous={}, input_kind='amend',
                           config=dict(E.defaults(), crew='l4'),
                           task_input=dict(id='task', kind='amend'))
        followup = Msg('user', 'New amendment')
        messages = [Msg('user', 'Original task'), followup]
        with (patch('loop.legacy_entry._plan_first', return_value='Investigate existing API') as plan,
              patch('loop.scoped_roles.check_handoff', return_value=[])):
            system, task, parts = entry.prepare(Mock(), 'BASE', 'Original task',
                [('task','Original task')], messages, followup, 'New amendment', None, None, [])
        self.assertEqual(plan.call_count, 1)
        self.assertEqual(system, 'BASE')
        self.assertEqual(messages[-1], followup)
        self.assertEqual(messages[-2].role, 'assistant')
        self.assertIn('Investigate existing API', messages[-2].content)

if __name__ == '__main__':
    unittest.main()
