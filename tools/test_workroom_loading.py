"""Selective workroom tools, lossless original requirements, unambiguous IDs."""
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from loop import executor_core as EC, executor_workflow as EW, task_input as TI, code_service as C
from loop.tools import default_registry
from loop import loop as L
from loop.provider import MockProvider
from tools import test_l0_foundation as foundation, test_l0_tool_slimming as slim


class WorkroomSurfaceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = foundation.L0WorkTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.session, self.root, self.core = self.fixture.session, self.fixture.root, self.fixture.core

    def registry(self, core=None):
        registry = default_registry(self.root)
        (core or self.core).install(registry)
        return registry

    @staticmethod
    def names(registry):
        return {t['name'] for t in registry.schemas()}

    def test_plain_and_verification_tasks_do_not_load_workroom_or_its_check_parameters(self):
        registry = self.registry()
        for stage in ('plain', 'verification'):
            if stage == 'verification':
                registry.get('use_tool_group').fn('verification')
            self.assertNotIn('test_workroom', self.names(registry), stage)
            self.assertNotIn('review_test_environment', self.names(registry), stage)
            check = registry.get('run_project_check')
            self.assertIn('script', check.parameters['properties'])
            for name in ('scenario', 'parameters', 'environment_id'):
                self.assertNotIn(name, check.parameters['properties'], stage)
        self.assertIn('inspect_delivery', self.names(registry))
        self.assertIsNotNone(registry.get('test_workroom'), 'old history retains guarded handler')

    def test_explicit_obligations_do_not_implicitly_load_workroom(self):
        with patch('loop.delivery_review.load', return_value=dict(requirements=[dict(required=True, logicalTaskId='task')])):
            registry = self.registry()
        self.assertIn('inspect_delivery', self.names(registry))
        self.assertIn('record_delivery_evidence', self.names(registry))
        self.assertNotIn('test_workroom', self.names(registry))

    def test_workroom_loads_only_its_tools_and_survives_same_task_resume(self):
        registry = self.registry()
        result = registry.get('use_tool_group').fn('workroom')
        self.assertEqual(set(result['tools']), {'test_workroom', 'review_test_environment'})
        self.assertNotIn('inspect_delivery', self.names(registry))
        for name in ('scenario', 'parameters', 'environment_id'):
            self.assertIn(name, registry.get('run_project_check').parameters['properties'])
        before = deepcopy(registry.schemas())
        registry.get('use_tool_group').fn('workroom')
        self.assertEqual(registry.schemas(), before)
        resumed = EC.ExecutorCore(self.session, self.root)
        self.assertEqual(self.registry(resumed).schemas(), before)
        self.session.task_input = dict(id='new-task', revision=1)
        new = self.registry(EC.ExecutorCore(self.session, self.root))
        self.assertNotIn('test_workroom', self.names(new))
        self.assertNotIn('environment_id', new.get('run_project_check').parameters['properties'])

    def test_plain_requirements_roundtrip_and_pagination_preserve_source_and_obligations(self):
        source = 'code = "\\n"\r\nprint("引号\\\\😀")\n' * 800
        material = dict(taskId='task', revision=2, entries=[dict(kind='goal', text=source),
                        dict(kind='amend', text='保留原公共输入', question='需要兼容吗？')],
                        requirements=[dict(id='keep', text='保留公共输入', status='active'),
                                      dict(id='old', text='旧输出', status='withdrawn')])
        material['contentHash'] = TI.requirements_hash(material)
        before = deepcopy(material)
        self.session.effective_requirements = material
        owner = SimpleNamespace(state=dict(effectiveRequirements=material))
        expected = TI.effective_text(material)
        for reader in (self.core.requirements, lambda after=0: EW.requirements(owner, after)):
            chunks = []; cursor = 0
            while True:
                page = json.loads(json.dumps(reader(cursor)))
                self.assertEqual(page['contentHash'], material['contentHash'])
                self.assertEqual(page['taskId'], 'task')
                self.assertEqual(page['revision'], 2)
                self.assertEqual(page['entryCount'], 2)
                self.assertLessEqual(len(page['text']), 12000)
                chunks.append(page['text'])
                if not page['hasMore']:
                    break
                self.assertGreater(page['next'], cursor)
                cursor = page['next']
            self.assertEqual(''.join(chunks), expected)
            self.assertIn(source, ''.join(chunks))
            self.assertIn('已撤销', ''.join(chunks))
            for bad in (-1, True, '0', len(expected)+1):
                with self.subTest(cursor=bad), self.assertRaises(EC.W.Problem):
                    reader(bad)
        self.assertEqual(material, before)

    def test_bounded_requirement_page_reaches_next_model_without_second_spill(self):
        material = dict(taskId='task', revision=1, entries=[dict(kind='goal',
                        text='value = "引号\\n😀"\r\n'*1000)])
        self.session.effective_requirements = material
        provider = MockProvider([dict(tool='read_task_requirements', args={}), dict(text='Original page received')])
        with patch('loop.code_service.executor_verification', return_value=None):
            L.run('Read the original requirement page', provider, self.registry(), lambda _:dict(ok=True),
                  session=self.session, max_turns=2, executor_only=True, execution_core=self.core)
        page = next(json.loads(m['content']) for m in self.session.load()['msgs'] if m['role']=='tool')
        self.assertNotIn('out_ref', page)
        self.assertEqual(page['text'], TI.effective_text(material)[:12000])


class EnvironmentReceiptTests(unittest.TestCase):
    def test_well_formed_id_still_requires_native_task_scope_and_never_launches(self):
        fixture = slim.ToolSlimmingTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with patch.object(C.W, 'launch', side_effect=AssertionError('unauthorized launch')) as launch:
            result = C.invoke(fixture.execution_root, 'check', fixture.r['id'],
                              script='assert True', environment_id='a'*32)
        self.assertEqual(result['code'], 'invalid_scope')
        launch.assert_not_called()

    def test_bad_environment_id_is_rejected_before_resource_or_git_access(self):
        for value in ('f'*64, 'other', '', True, 12):
            with self.subTest(value=value), patch.object(C, 'context_resource', side_effect=AssertionError('late validation')) as context:
                result = C.invoke('.', 'check', 'resource', script='assert True', environment_id=value)
                self.assertEqual(result['code'], 'invalid_arguments')
                self.assertIn('environment_id', result['error'])
                self.assertIn('指纹', result['error'])
                self.assertEqual(result.get('operationCosts', {}).get('gitProcesses', 0), 0)
                context.assert_not_called()

    def test_model_receipt_separates_fingerprint_from_reusable_id_without_changing_evidence(self):
        raw = dict(ok=False, environment='f'*64, process=dict(id='process', state='failed', exitCode=1,
                   environmentId='a'*32), diagnostics=dict(keyErrors=[dict(text='REAL_FAILURE')]),
                   verification=dict(status='failed', revision='v1'))
        before = deepcopy(raw)
        view = EC.ExecutorCore.model_result(None, 'run_project_check', raw)
        self.assertNotIn('environment', view)
        self.assertEqual(view['environmentFingerprint'], 'f'*64)
        self.assertEqual(view['environment_id'], 'a'*32)
        # The model receives execution facts, while the host keeps the original verification record.
        self.assertNotIn('verification', view)
        self.assertEqual(view['execution']['sourceRevision'], 'v1')
        self.assertEqual(view['process']['state'], 'failed')
        self.assertEqual(view['process']['exitCode'], 1)
        self.assertEqual(view['diagnostics']['messages'], [{'text': 'REAL_FAILURE'}])
        self.assertEqual(raw, before)
        raw['process'].pop('environmentId')
        view = EC.ExecutorCore.model_result(None, 'run_project_check', raw)
        self.assertNotIn('environment_id', view)


if __name__ == '__main__':
    unittest.main()
