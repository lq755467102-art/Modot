"""Exercise the project editor, scoped search, terminal, and test runner on the same task view."""
import os
from pathlib import Path
import unittest

from loop import code_service as C, project_shell, project_task_view as views, workbench as W
from loop.tools import current_call
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
