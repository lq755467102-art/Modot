"""Verify the backing files independently of staged reads and command receipts."""
import os
import subprocess
import sys
from pathlib import Path
import unittest
from unittest.mock import patch

from loop import project_transaction as transactions
from loop import workbench as W
from tools.test_conversations import ConversationFixture


@unittest.skipUnless(os.name == "nt", "Windows checkout and process identities")
class ProjectTransactions(ConversationFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.project = self.root / "project"
        self.project.mkdir()
        self.source = self.project / "source.txt"
        self.source.write_bytes(b"original")
        self.store = self.root / "transactions"

    def view(self, name="one"):
        return transactions.View.create(self.store, self.project, name)

    def test_read_through_and_byte_writes_touch_only_the_selected_files(self):
        big = self.project / "unused.bin"
        with big.open("wb") as handle:
            handle.truncate(540 * 1024 * 1024)
        v = self.view()
        self.assertEqual(v.read("unused.bin", big.stat().st_size - 1, 1), b"\0")
        self.assertEqual(v.changes(), {})
        v.write("source.txt", 0, b"changed!")
        self.assertEqual(v.read("source.txt", 0, 8), b"changed!")
        self.assertEqual(self.source.read_bytes(), b"original")
        stamp = big.stat().st_mtime_ns
        v.replace("new.txt", b"new")
        self.assertCountEqual(v.changes(), ["new.txt", "source.txt"])
        self.assertCountEqual(v.publish(lambda: None), ["new.txt", "source.txt"])
        self.assertEqual(self.source.read_bytes(), b"changed!")
        self.assertEqual(big.stat().st_mtime_ns, stamp)
        self.assertFalse(v.directory.exists())

    def test_failed_run_and_revocation_keep_original_and_other_task(self):
        one, two = self.view(), self.view("two")
        one.replace("source.txt", b"test failed")
        two.replace("source.txt", b"other")
        one.discard()
        self.assertEqual(self.source.read_bytes(), b"original")
        self.assertEqual(two.read("source.txt", 0, 9), b"other")
        def revoked():
            raise W.Problem("resource_denied", "revoked", 403)
        with self.assertRaises(W.Problem):
            two.publish(revoked)
        self.assertEqual(self.source.read_bytes(), b"original")
        two.discard()
        self.assertFalse(two.directory.exists())

    def test_external_change_is_not_overwritten(self):
        v = self.view()
        v.replace("source.txt", b"candidate")
        v.replace("other.txt", b"candidate")
        self.source.write_bytes(b"external")
        with self.assertRaises(W.Problem):
            v.publish(lambda: None)
        self.assertFalse((self.project / "other.txt").exists())
        self.assertEqual(self.source.read_bytes(), b"external")
        v.discard()

    def test_process_crash_scavenges_only_its_staging(self):
        v = self.view("live")
        v.replace("source.txt", b"live")
        code = ("import sys,os;from pathlib import Path;sys.path.insert(0,sys.argv[1]);"
                "from loop.project_transaction import View;"
                "v=View.create(Path(sys.argv[2]),Path(sys.argv[3]),'crashed');"
                "v.replace('source.txt',b'crash');print(v.directory,flush=True);os._exit(33)")
        sub = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(Path(__file__).resolve().parents[1]),
                              str(self.store), str(self.project)], capture_output=True, text=True, check=False)
        self.assertEqual(sub.returncode, 33, sub.stderr)
        dead = Path(sub.stdout.strip())
        self.assertTrue(dead.is_dir())
        foreign = self.store / "unregistered"
        foreign.mkdir()
        transactions.recover(self.store)
        self.assertFalse(dead.exists())
        self.assertTrue(foreign.exists())
        self.assertEqual(v.read("source.txt", 0, 9), b"live")
        self.assertEqual(self.source.read_bytes(), b"original")
        v.discard()

    def test_revoked_after_first_file_is_rolled_back(self):
        v = self.view()
        v.replace("a.txt", b"a")
        v.replace("b.txt", b"b")
        def live():
            if (self.project / "a.txt").exists():
                raise W.Problem("approval_revoked", "permission changed", 403)
        with self.assertRaises(W.Problem):
            v.publish(live)
        self.assertFalse((self.project / "a.txt").exists())
        self.assertFalse((self.project / "b.txt").exists())
        v.discard()

    def test_crash_during_publish_rolls_back_only_its_own_changes(self):
        live = self.view("other")
        live.replace("other.txt", b"other candidate")
        try:
            script = """import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from loop.project_transaction import View
store, root = Path(sys.argv[2]), Path(sys.argv[3])
v = View.create(store, root, "interrupted")
v.replace("a.txt", b"one")
v.replace("b.txt", b"two")
v.publish(lambda: os._exit(33) if (root / "a.txt").is_file() else None)
"""
            proc = subprocess.run([sys.executable, "-I", "-B", "-c", script,
                                   str(Path(__file__).resolve().parents[1]), str(self.store), str(self.project)],
                                  capture_output=True, text=True, check=False)
            self.assertEqual(proc.returncode, 33, proc.stderr)
            self.assertTrue((self.project / "a.txt").is_file())
            self.assertFalse((self.project / "b.txt").exists())
            self.assertEqual(len(transactions.recover(self.store)), 1)
            self.assertFalse((self.project / "a.txt").exists())
            self.assertEqual(live.read("other.txt", 0, 30), b"other candidate")
        finally:
            live.discard()

    def test_undo_and_quota_do_not_destroy_the_previous_candidate(self):
        v = self.view()
        try:
            v.edit("source.txt", "first", b"original", b"second")
            with patch.object(transactions.safe_executor, "MAX_BYTES", 21):
                with self.assertRaises(W.Problem):
                    v.edit("source.txt", "rejected", b"second", b"oversized replacement")
            self.assertEqual(v.read("source.txt", 0, 24), b"second")
            self.assertEqual(v.undo("first")["revision"], W.digest(b"original"))
            self.assertFalse(v.changes())
            v.edit("source.txt", "new", b"original", b"third")
            v.edit("source.txt", "latest", b"third", b"fourth")
            self.assertEqual(v.undo("latest")["revision"], W.digest(b"third"))
            v.replace("source.txt", b"child wrote this")
            with self.assertRaises(W.Problem):
                v.undo("new")
            self.assertEqual(self.source.read_bytes(), b"original")
        finally:
            v.discard()

    def test_unrelated_passing_test_cannot_erase_a_failed_suite(self):
        from loop.project_task_view import note_check
        v = self.view()
        try:
            v.replace("source.txt", b"candidate")
            note_check(v, "red", {"state": "failed"}, passed=False, check_key="suite A")
            note_check(v, "green", {"state": "passed"}, passed=True, check_key="suite B")
            self.assertIsNone(v.verified)
            note_check(v, "retry", {"state": "passed"}, passed=True, check_key="suite A")
            self.assertEqual(v.verified, v.revision())
            v.replace("source.txt", b"later edit")
            self.assertIsNone(v.verified)
        finally:
            v.discard()

    def test_disposable_check_reads_its_parent_without_changing_it(self):
        from loop.project_task_view import disposable
        v = self.view()
        v.replace("source.txt", b"edit")
        v.mkdir("new-dir")
        v.replace("new-dir/new.txt", b"draft")
        child = disposable(v)
        try:
            self.assertEqual(child.read("new-dir/new.txt", 0, 5), b"draft")
            self.assertIn("new-dir", child.list())
            child.replace("source.txt", b"test only")
            self.assertEqual(v.read("source.txt", 0, 30), b"edit")
            with self.assertRaises(W.Problem):
                child.publish(lambda: None)
        finally:
            child.discard()
            v.discard()
        self.assertEqual(self.source.read_bytes(), b"original")
