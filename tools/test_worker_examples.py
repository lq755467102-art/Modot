"""Exercise shipped, data-only examples using the public package/contract APIs."""
import json
import os
from unittest.mock import patch
from pathlib import Path
from unittest import TestCase

from loop import models, plugins, projects, worker_contract, worker_plugins, workbench
from tools.test_conversations import ConversationFixture


EXAMPLES = Path(__file__).resolve().parents[1] / 'examples'


class OfficialWorkerExamples(ConversationFixture, TestCase):
    def test_shipped_inputs_results_and_boundaries(self):
        for folder in ('worker-planner', 'worker-scout', 'worker-vision'):
            with self.subTest(folder=folder):
                path = EXAMPLES / folder
                source = plugins.catalog_source('vision') if folder == 'worker-vision' else path
                worker, = plugins.preview(str(source))['workers']
                sample = json.loads((path / 'example.json').read_text(encoding='utf8'))
                worker_contract.validate(sample['input'], worker['inputSchema'])
                worker_contract.validate(sample['output'], worker['outputSchema'])
                self.assertTrue({'isolated-write', 'isolated-check'}.isdisjoint(worker['requires'] + worker['optional']))
                with self.assertRaises(workbench.Problem):
                    worker_contract.validate(dict(sample['output'], disposition='accepted'), worker['outputSchema'])
                with self.assertRaises(workbench.Problem):
                    worker_contract.validate({}, worker['inputSchema'])

    def test_vision_preset_uses_the_selected_connection_and_starts_disabled(self):
        endpoint = '/api/v2/plugins/presets/vision'

        def preset(body=None, expected_status=200):
            status, result = workbench.handle('GET' if body is None else 'POST', endpoint, {}, body or {}, self.sv)
            self.assertEqual(status, expected_status)
            return result

        def connect(name, endpoint, model):
            response = models.user_action(dict(act='save', name=name, base_url=endpoint,
                                               model=model, api_key='fixture-key'))
            self.assertTrue(response['ok'])
            return response['id']

        with (patch.object(models, 'path', return_value=self.root / 'models.json'),
              patch.object(models, '_creds', return_value={}),
              patch.dict(os.environ, {'HARNESS_VISION_API_KEY': '', 'HARNESS_VISION_MODEL': '',
                                      'HARNESS_VISION_BASE_URL': ''})):
            initial = preset()
            self.assertFalse(initial['enabled'])
            self.assertEqual(initial['modelState'], 'worker_model_missing')
            self.assertEqual(plugins.listing()['total'], 0)
            main = connect('main', 'https://api.deepseek.com/v1', 'fixture-main')
            first = connect('visual-a', 'https://open.bigmodel.cn/api/paas/v4', 'fixture-vision-a')
            second = connect('visual-b', 'https://vision-fixture.example/v1', 'fixture-vision-b')
            for role, ident in [('main', main), ('vision', first)]:
                self.assertTrue(models.user_action(dict(act='select', role=role, id=ident))['ok'])

            enable = dict(enabled=True, fingerprint=initial['fingerprint'], expectedRevision=initial['revision'])
            enabled = preset(enable)
            self.assertTrue(enabled['enabled'])
            self.assertEqual(enabled['model'], dict(role='vision', model='fixture-vision-a', provider='智谱 GLM'))
            self.assertEqual(preset(enable), enabled)
            self.assertTrue(models.user_action(dict(act='select', role='vision', id=second))['ok'])
            changed = preset()
            self.assertEqual(changed['model']['model'], 'fixture-vision-b')
            self.assertEqual(changed['model']['provider'], '自定义兼容端点')
            self.assertEqual(models.public()['main'], main)

            self.assertTrue(models.user_action(dict(act='select', role='vision', id=main))['ok'])
            unavailable = preset()
            self.assertIsNone(unavailable['model'])
            self.assertEqual(unavailable['modelState'], 'worker_model_same')
            stale = preset(dict(enable, enabled=False), expected_status=409)
            self.assertEqual(stale['code'], 'plugin_changed')
            disabled = preset(dict(enable, enabled=False, expectedRevision=enabled['revision']))
            self.assertFalse(disabled['enabled'])
            self.assertNotIn('fixture-key', json.dumps(disabled))

    def test_independent_registration_including_renamed_third_party_copy(self):
        _, parent = self.call('POST')
        contributions = []
        expected = set()
        for folder in ('worker-planner', 'worker-scout', 'third-party'):
            path = EXAMPLES / folder
            if folder == 'third-party':
                path = self.root / folder
                path.mkdir()
                manifest = json.loads((EXAMPLES / 'worker-scout/plugin.json').read_text(encoding='utf8'))
                manifest['name'] = 'independent-reference'
                manifest['workers'][0]['name'] = 'research'
                (path / 'plugin.json').write_text(json.dumps(manifest), encoding='utf8')
            preview = plugins.preview(str(path))
            installed = plugins.install(str(path), preview['fingerprint'])
            self.assertFalse(installed['workersEnabled'])
            enabled = plugins.change(installed['id'], dict(expectedRevision=installed['revision'], workersEnabled=True, confirm=True))
            contributions.append(enabled)
            expected.add(installed['id'] + '/' + preview['workers'][0]['name'])
        projects.snapshot(parent['id'])

        scout = next(w for w in worker_plugins.discover(parent['id'])['workers']
                     if w['worker'].endswith('/investigate'))
        fields = scout['inputSchema']['properties']
        self.assertIn('一个', fields['question']['description'])
        self.assertIn('主执行者', fields['scope']['description'])
        self.assertIn('已查证据', fields['tried']['description'])

        def visible():
            entries = worker_plugins.discover(parent['id'])['workers']
            self.assertTrue(all('method' not in w for w in entries))
            return {w['worker'] for w in entries}

        self.assertEqual(visible(), expected)
        for package in contributions:
            plugins.change(package['id'], dict(expectedRevision=package['revision'], remove=True))
            expected.remove(package['id'] + '/' + package['workers'][0]['name'])
            self.assertEqual(visible(), expected)
