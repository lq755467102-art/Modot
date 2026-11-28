"""Exercise the project editor, scoped search, terminal, and test runner on the same task view."""
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import uuid4

from loop import code_service as C, project_shell, project_task_view as views, workbench as W
from loop.tools import current_call, set_call, reset_call
from tools import test_checkout_permissions as fixtures


@unittest.skipUnless(os.name == 'nt' and (Path(os.environ.get('WINDIR',''))/'System32/dokan2.dll').is_file(),
                     'uses a real Dokan 2 mount and Windows AppContainer')
class OverlayActions(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.CheckoutPermissions('test_open_existing_project_in_each_preset')
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.chat = self.case.chat('ask')
        self.case.start(self.chat)
        self.folder = self.case.folder
        self.rid = self.case.rid

    def test_edit_search_check_and_publish(self):
        with self.case.owner(self.chat) as root:
            created = C.invoke(root, 'write', self.rid, relative_path='candidate.txt', create=True, text='candidate-123')
            self.assertTrue(created.get('staged'),created)
            self.assertFalse((self.folder/'candidate.txt').exists())
            inspected = C.invoke(root,'inspect',self.rid,relative_path='candidate.txt')
            self.assertEqual(inspected.get('text'),'candidate-123',inspected)
            searched = C.invoke(root,'search',self.rid,query='candidate-123',relative_path='candidate.txt')
            self.assertTrue(searched.get('matches'),searched)
            script = ("import unittest\nfrom pathlib import Path\nclass T(unittest.TestCase):\n"
                      " def test_candidate(self):\n  self.assertEqual(Path('candidate.txt').read_text(),'candidate-123')\n"
                      "unittest.main()\n")
            check = C.invoke(root,'check',self.rid,script=script,timeout=30)
            self.assertTrue(check.get('ok'),check)
            v = views.lookup(self.chat,self.rid)
            self.assertIsNotNone(v.verified,check)
            self.assertFalse((self.folder/'candidate.txt').exists())
            outcome = views.settle(self.chat,current_call()['runId'],'success')
            self.assertEqual(outcome[0]['state'],'published',outcome)
            self.assertEqual(outcome[0]['paths'],['candidate.txt'])
            self.assertEqual((self.folder/'candidate.txt').read_text(),'candidate-123')
            self.assertFalse((self.folder/'.harness-check').exists())

    def test_tool_undo_stays_inside_the_staged_view(self):
        with self.case.owner(self.chat) as root:
            created = C.invoke(root, 'write', self.rid, relative_path='trial.txt', create=True, text='trial')
            self.assertTrue(created.get('staged'), created)
            removed = C.invoke(root, 'restore', self.rid, source_operation_id=created['operationId'])
            self.assertTrue(removed.get('staged'), removed)
            v = views.lookup(self.chat, self.rid)
            self.assertEqual(v.changes(), {})
            self.assertFalse((self.folder / 'trial.txt').exists())
            self.assertEqual(views.settle(self.chat, current_call()['runId'], 'paused'), [])

    def test_hosted_check_reads_the_edited_revision_and_publishes_once(self):
        import json
        import urllib.request
        from io import BytesIO
        from loop import agent_gateway

        def host(request, **unused):
            body = json.loads(request.data)
            agent_gateway.validate_call(self.chat, body)
            if body.get('taskOwnsCheck') or body['operation'] != 'check-start':
                reply = agent_gateway.dispatch(self.chat, body, server=self.case.sv)
            else:
                # The desktop host cannot see the runner's process-local transaction.
                reply = dict(ok=False, code='different_project_view')
            return BytesIO(json.dumps(dict(reply, request_id=agent_gateway.request_id(body['context']))).encode())

        with self.case.owner(self.chat) as root:
            state = current_call()
            prior = C.invoke(root, 'inspect', self.rid, relative_path='existing.txt')
            edit = C.invoke(root, 'edit', self.rid, relative_path='existing.txt',
                            expected_revision=prior['revision'], old_text='original', new_text='updated')
            self.assertTrue(edit.get('staged'), edit)
            self.addCleanup(views.settle, self.chat, state['runId'], 'paused')
            revision = views.lookup(self.chat, self.rid).revision()
            script = ("import unittest\nfrom pathlib import Path\nclass T(unittest.TestCase):\n"
                      " def test_revision(self):\n  self.assertEqual(Path('existing.txt').read_text(),'updated')\n"
                      "unittest.main()\n")
            with (patch.dict(os.environ, HARNESS_AGENT_URL='http://127.0.0.1:1/api/agent-tools',
                             HARNESS_AGENT_TOKEN=self.chat + ':' + uuid4().hex),
                  patch.object(C.RuntimeView, '_resolve_sid', side_effect=self.case.sv._resolve_sid),
                  patch.object(urllib.request.OpenerDirector, 'open', side_effect=host)):
                def invoke(name, operation, **args):
                    token = set_call(dict(state, actionId=uuid4().hex, tool=name))
                    try:
                        return C.adapter(root, operation)(self.rid, **args)
                    finally:
                        reset_call(token)
                started = invoke('run_project_check', 'check', script=script, timeout=30, yield_time_ms=0)
                self.assertIn('operationId', started, started)
                self.assertEqual((self.folder / 'existing.txt').read_text(), 'original')
                result = invoke('read_check_output', 'output', operation_id=started['operationId'], wait_ms=30000)
                self.assertTrue(result.get('ok'), result)
                self.assertEqual(result['verification']['revision'], revision)
            outcome = views.settle(self.chat, state['runId'], 'success')
            self.assertEqual(outcome[0]['paths'], ['existing.txt'], outcome)
            self.assertEqual((self.folder / 'existing.txt').read_text(), 'updated')

    def test_check_without_edits_releases_only_its_own_stage(self):
        from loop import check_sessions
        with self.case.owner(self.chat) as root:
            run = current_call()
            with views.check_scope(self.chat, self.rid):
                target = C.context_resource(root, self.rid, 'execute')
                temporary = views.for_action(root, *target, 'execute')
                self.assertTrue(temporary.marker.is_file())
            self.assertIsNone(views.lookup(self.chat, self.rid))
            self.assertFalse(temporary.marker.exists())
            self.assertFalse(check_sessions.task_owns_check('check-start', run, dict(resource_id=self.rid)))

    def test_failed_child_and_bare_zero_do_not_publish(self):
        for mode in ('fail','exit-zero'):
            with self.subTest(mode=mode), self.case.owner(self.chat) as root:
                fn = mode+'.txt'
                self.assertTrue(C.invoke(root,'write',self.rid,relative_path=fn,create=True,text='pending')['ok'])
                command = "import sys;sys.exit(1)" if mode=='fail' else "print('no tests')"
                result = project_shell.run(root,self.rid,['python','-c',command])
                self.assertEqual(result['exitCode'],1 if mode=='fail' else 0,result)
                outcome = views.settle(self.chat,current_call()['runId'],'success')
                self.assertEqual(outcome[0]['state'],'discarded',outcome)
                self.assertFalse((self.folder/fn).exists())
