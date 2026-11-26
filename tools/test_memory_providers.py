"""Memory provider lifetime at the host/config/tool seams, no model network."""
import unittest
from pathlib import Path
from unittest.mock import patch
from tools import test_memory_default as fixtures
from loop import memory_service as M, plugins


class Providers(unittest.TestCase):
    def setUp(self):
        f = self.f = fixtures.DefaultMemory()
        f.setUp()
        self.addCleanup(f.doCleanups)
        from loop import memory_providers
        self.P = memory_providers

    def change(self, **kw):
        return self.P.change(dict(expectedRevision=self.P.settings()['revision'], **kw))

    def test_default_is_usable_without_install_or_configuration(self):
        lease = self.P.acquire(self.f.a)
        self.assertEqual(lease.identity['id'], self.P.BUILTIN)
        self.assertTrue(lease.available)
        self.assertTrue(lease.adapter.capabilities['promote'])

    def test_uninstall_has_no_fallback_and_does_not_remove_data(self):
        saved = self.f.collect()
        before = M.node_target(self.f.a, saved['id'])[0].path.read_bytes()
        lease = self.P.acquire(self.f.a)
        self.change(operation='uninstall-builtin')
        self.assertFalse(lease.available)
        self.assertFalse(self.P.acquire(self.f.a).available)
        called = []
        result = lease.execute(lambda: called.append(True), write=True)
        self.assertEqual(result['memoryWrite']['status'], 'not_saved')
        self.assertEqual(called, [])
        self.assertEqual(M.node_target(self.f.a, saved['id'])[0].path.read_bytes(), before)
        self.assertFalse(self.P.settings()['builtinInstalled'])

    def test_aba_does_not_revive_old_handle(self):
        lease = self.P.acquire(self.f.a)
        self.change(providerId=None)
        self.change(providerId=self.P.BUILTIN)
        self.assertFalse(lease.available)
        self.assertTrue(self.P.acquire(self.f.a).available)

    def test_live_revocation_discards_late_read_body(self):
        lease = self.P.acquire(self.f.a)
        def late():
            self.change(providerId=None)
            return dict(ok=True, nodes=[dict(body='MUST_NOT_LEAK_FROM_REVOKED_PROVIDER')])
        result = lease.execute(late)
        self.assertNotIn('MUST_NOT_LEAK', str(result))
        self.assertEqual(result['code'], 'memory_unavailable')

    def test_stale_configuration_cannot_restore_or_change_provider(self):
        revision = self.P.settings()['revision']
        self.change(providerId=None)
        with self.assertRaises(M.W.Problem):
            self.P.change(dict(expectedRevision=revision, providerId=self.P.BUILTIN))

    def notes(self):
        root = Path(__file__).resolve().parents[1]/'examples/memory-notes'
        package = plugins.install(str(root), plugins.preview(str(root))['fingerprint'])
        self.change(providerId=package['id'])
        return package

    def test_notes_use_owned_session_storage_and_do_not_touch_tree(self):
        original = self.f.collect()
        default_path = M.node_target(self.f.a, original['id'])[0].path
        before = default_path.read_bytes()
        package = self.notes()
        lease = self.P.acquire(self.f.a)
        saved = lease.execute(lambda:lease.adapter.mutate(dict(title='Note',body='SESSION_NOTE',expectedRevision=0)),write=True)
        self.assertEqual(saved['memoryWrite']['status'], 'saved')
        self.assertEqual(self.P.acquire(self.f.a).adapter.view()['nodes'][0]['body'], 'SESSION_NOTE')
        self.assertEqual(self.P.acquire(self.f.b).adapter.view()['nodes'], [])
        self.assertEqual(default_path.read_bytes(), before)
        self.assertFalse(lease.adapter.capabilities['promote'])
        self.change(providerId=self.P.BUILTIN)
        self.assertTrue(self.P.acquire(self.f.a).adapter.view()['nodes'])
        self.change(providerId=package['id'])
        self.assertEqual(self.P.acquire(self.f.a).adapter.view()['nodes'][0]['id'], saved['id'])

    def test_notes_cas_and_unsupported_operation_do_not_fall_back(self):
        self.notes()
        lease = self.P.acquire(self.f.a)
        first = lease.execute(lambda:lease.adapter.mutate(dict(title='N',body='Original',expectedRevision=0)),write=True)
        changed = lease.execute(lambda:lease.adapter.mutate(dict(id=first['id'],title='N',body='New',expectedRevision=1,expectedVersion=1)),write=True)
        self.assertEqual(changed['version'],2)
        failed = lease.execute(lambda:lease.adapter.mutate(dict(id=first['id'],title='N',body='Wrong',expectedRevision=1)),write=True)
        self.assertEqual(failed['code'],'memory_stale')
        self.assertEqual(failed['memoryWrite']['status'],'not_saved')
        unsupported = lease.execute(lambda:lease.adapter.mutate(dict(operation='promote',id=first['id'])),write=True)
        self.assertEqual(unsupported['code'],'memory_unsupported')
        self.assertEqual(lease.adapter.view()['nodes'][0]['body'],'New')
        self.assertFalse(M.target(self.f.a,scope='session')[0].path.exists())

    def test_data_plugin_cannot_choose_paths_or_execute_code(self):
        for value in [dict(adapter='session-notes',path='C:/'),dict(adapter='python:dangerous'),dict(adapter='session-notes',scope='user')]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.P.declaration(value)

    def test_no_provider_strips_memory_and_retains_original_task_and_charter_rules(self):
        from loop import memory_runtime as R
        from loop.context_manager import RequestBody
        self.f.collect()
        s, _ = self.f.owner(self.f.a)
        raw = dict(messages=[dict(role='system',content='BASE'),dict(role='user',content='ORIGINAL_USER_TASK')],
            tools=[dict(type='function',function=dict(name=n)) for n in ('remember','read_memory_tree','inspect_code')])
        before = R.prepare_body(s,'executor',raw)
        self.assertIn('A reusable fact',str(before))
        self.change(providerId=None)
        charter='【本轮约束 · 由 harness 每轮注入，不是用户说的话】\n## 习惯（跨项目，可被下面的项目规则覆盖）\n### group\nOLD_HABIT\n## Project rules\nKEEP_PROJECT_RULE\n【约束结束，以下才是本轮的实际内容】\nKEEP_AFTER_CHARTER'
        before['messages'].insert(0,dict(role='system',content=charter))
        after=R.prepare_body(s,'executor',before)
        self.assertNotIn('A reusable fact',str(after))
        self.assertNotIn('OLD_HABIT',str(after))
        self.assertIn('ORIGINAL_USER_TASK',str(after))
        self.assertIn('KEEP_PROJECT_RULE',str(after))
        self.assertIn('KEEP_AFTER_CHARTER',str(after))
        self.assertEqual([t['function']['name'] for t in after['tools']],['inspect_code'])
        self.assertFalse(R.capture(s,'executor',after)['loaded'])

    def test_removed_plugin_is_not_reenabled_on_restart_or_reinstall(self):
        package = self.notes()
        lease = self.P.acquire(self.f.a)
        live = plugins.state()['plugins'][package['id']]
        plugins.change(package['id'],dict(expectedRevision=live['revision'],remove=True))
        self.assertIsNone(self.P.settings()['providerId'])
        self.assertFalse(lease.available)
        source=Path(__file__).resolve().parents[1]/'examples/memory-notes'
        plugins.install(str(source),plugins.preview(str(source))['fingerprint'])
        self.assertIsNone(self.P.settings()['providerId'])

    def test_unregister_adapter_revokes_handles_without_deleting_its_data(self):
        class Tiny(self.P.TreeAdapter):
            pass
        dispose=self.P.register_adapter('temporary',Tiny)
        identity=dict(self.P.acquire(self.f.a).identity,adapter='temporary')
        lease=self.P.acquire(self.f.a,identity=identity)
        self.assertTrue(lease.available)
        dispose();dispose()
        self.assertFalse(lease.available)
        self.assertEqual(lease.execute(lambda:dict(body='STALE'))['nodes'],[])

    def test_unknown_write_is_not_replayed(self):
        lease=self.P.acquire(self.f.a)
        attempts=[]
        def uncertain():
            attempts.append(1)
            raise OSError('result lost')
        result=lease.execute(uncertain,write=True)
        self.assertEqual(result['memoryWrite']['status'],'unknown')
        self.assertEqual(attempts,[1])

    def test_revocation_after_write_does_not_claim_nothing_was_saved(self):
        class Temporary(self.P.TreeAdapter):
            pass
        dispose=self.P.register_adapter('late-writer',Temporary)
        identity=dict(self.P.acquire(self.f.a).identity,adapter='late-writer')
        lease=self.P.acquire(self.f.a,identity=identity)
        effects=[]
        def publish_then_close():
            effects.append('published')
            dispose()
            return dict(ok=True,id='original-provider-entry')
        result=lease.execute(publish_then_close,write=True)
        self.assertEqual(effects,['published'])
        self.assertEqual(result['memoryWrite']['status'],'unknown')

    def test_published_adoption_with_failed_source_receipt_is_unknown_and_replay_safe(self):
        from loop.memory_tree import TreeStore
        node=self.f.collect()
        self.f.event(node['id'],'applied',conversationId=self.f.a,note='First independent use')
        self.f.event(node['id'],'applied',conversationId=self.f.b,note='Second independent use')
        self.f.event(node['id'],'human-confirmed')
        proposal=self.f.promote(node['id'])
        source=M.node_target(self.f.a,node['id'])[0].path
        original=TreeStore.change
        def interrupt(store,expected,actor,fn):
            if store.path == source:
                def partial(row):
                    fn(row)
                    raise M.W.Problem('memory_capacity','source receipt failed',409)
                return original(store,expected,actor,partial)
            return original(store,expected,actor,fn)
        lease=self.P.acquire(self.f.a)
        with patch.object(TreeStore,'change',interrupt):
            receipt=lease.execute(lambda:self.f.adopt(proposal),write=True)
        self.assertEqual(receipt['memoryWrite']['status'],'unknown')
        self.assertEqual(len(self.f.view(self.f.b)['nodes']),1)
        self.assertTrue(self.f.adopt(proposal)['duplicate'])

    def test_corrupt_notes_remain_on_disk_but_do_not_stop_context_building(self):
        from loop import memory_runtime as R
        import json
        self.notes()
        lease=self.P.acquire(self.f.a)
        saved=lease.execute(lambda:lease.adapter.mutate(dict(title='N',body='Keep evidence')),write=True)
        store=lease.adapter.store()
        row=json.loads(store.path.read_text(encoding='utf8'))
        row['value']={'invalid':'KEEP_ORIGINAL_BYTES'}
        raw=json.dumps(row).encode('utf8');store.path.write_bytes(raw)
        session,_=self.f.owner(self.f.a)
        # The host captures the active plugin for this fixed request.
        M.W.change_registry(lambda data:data['threads'][self.f.a].setdefault('resourceSnapshot',{}).update(plugins=plugins.capture()))
        body=R.prepare_body(session,'executor',dict(messages=[dict(role='user',content='ORDINARY_TASK')],tools=[]))
        self.assertIn('ORDINARY_TASK',str(body))
        self.assertNotIn('Keep evidence',str(body))
        self.assertFalse(session.memory_provider_ready)
        self.assertEqual(store.path.read_bytes(),raw)

    def test_invalid_provider_config_is_not_reset_or_a_reason_to_require_a_memory_loop(self):
        path=plugins.directory()/'installed.json'
        path.write_text('{invalid-json',encoding='utf8')
        lease=self.P.acquire(self.f.a)
        self.assertFalse(lease.available)
        self.assertEqual(path.read_text(encoding='utf8'),'{invalid-json')
        self.assertIn('不可读取',self.P.empty_view(self.f.a,lease=lease)['notice'])


if __name__ == '__main__':
    unittest.main()
