"""Windows lifecycle fixtures. Every journal, directory and profile is disposable."""

import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
import uuid
import sys
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from loop import contain, runtime, safe_executor, sandbox_lifecycle as lifecycle
from loop import windows_sandbox_evidence as windows
from loop.filelock import atomic_json


@unittest.skipUnless(os.name == "nt", "requires Windows AppContainer and directory identities")
class SandboxLifecycle(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.temp = self.root / "temp"
        self.temp.mkdir()
        self.data = self.root / "profile-a"
        self.data.mkdir()
        self.index = self.root / "ownership"
        self.enterContext(patch.dict(os.environ, HARNESS_SANDBOX_LIFECYCLE_ROOT=str(self.index),
                                     HARNESS_DATA=str(self.data)))
        self.enterContext(patch.object(tempfile, "tempdir", str(self.temp)))
        self.enterContext(patch.object(windows, "task_registrations", return_value=([], False)))

    def profile(self, name):
        profile = contain.ensure_profile(name=name)
        self.assertTrue(profile.get("ok"), profile)
        self.addCleanup(safe_executor._delete_profile, name)
        return windows.container_sid(name)

    def test_partial_copy_has_persisted_ownership_before_the_first_file(self):
        fixture = r'''
import os
from pathlib import Path
from unittest.mock import patch
from loop import safe_executor as S, sandbox_lifecycle as L

def copy(_source, target, **_kwargs):
    target.mkdir()
    (target / "python.exe").write_bytes(b"partial fixture")
    print(str(target.parent), flush=True)
    os._exit(23)

with (patch.object(S, "_acl"), patch.object(S, "_runtime_fingerprint", return_value="fixture"),
      patch.object(S, "_copy", side_effect=copy)):
    S._shared_python(Path.cwd(), False)
'''
        env = dict(os.environ, TEMP=str(self.temp), TMP=str(self.temp))
        result = subprocess.run([sys.executable, "-B", "-c", fixture], env=env,
                                text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 23, result.stderr)
        shared = Path(result.stdout.strip())
        entry = lifecycle._record(shared)
        self.assertTrue(shared.is_dir())
        self.assertTrue(entry.is_file())
        profile_b = self.root/"profile-b"
        profile_b.mkdir()
        report = lifecycle.recover(profile_b)
        self.assertEqual(report["outcomes"].get("removed"), 1)
        self.assertGreater(report["logicalBytes"], 0)
        self.assertFalse(shared.exists())
        self.assertFalse(entry.exists())

    def test_identity_unknown_and_active_use_protect_records_but_reused_pid_does_not(self):
        dead, _ = lifecycle.allocate("runtime")
        unknown, _ = lifecycle.allocate("runtime")
        locked, _ = lifecycle.allocate("runtime")
        active, _ = lifecycle.allocate("runtime")
        lifecycle._edit(dead, pid=80, identity="old")
        lifecycle._edit(unknown, pid=81, identity="old")
        lifecycle._edit(locked, pid=80, identity="old")
        original = runtime.identity
        def who(pid):
            if pid == 80:
                return "different-creation"
            if pid == 81:
                return "unknown"
            return original(pid)
        lock = lifecycle._record(locked).stem
        with patch.object(runtime, "identity", side_effect=who), ThreadPoolExecutor(max_workers=1) as recovery:
            with lifecycle._lock(self.index, lock):
                result = recovery.submit(lifecycle.recover, self.data).result(5)
        self.assertFalse(dead.exists())
        self.assertTrue(unknown.exists())
        self.assertTrue(locked.exists())
        self.assertTrue(active.exists())
        self.assertEqual(result["outcomes"]["owner_identity_unknown"], 1)
        self.assertEqual(result["outcomes"]["owner_live"], 1)
        self.assertEqual(result["outcomes"]["locked"], 1)

    def test_lost_owner_does_not_override_living_child_or_shared_use(self):
        base, _ = lifecycle.allocate("sandbox")
        shared, _ = lifecycle.allocate("runtime")
        lifecycle.link_runtime(base, [shared/"python"])
        lifecycle.record_child(base, os.getpid(), runtime.identity(os.getpid()))
        lifecycle._edit(base, pid=2**31-1, identity="exited")
        lifecycle._edit(shared, pid=2**31-1, identity="exited")
        result = lifecycle.recover(self.data)
        self.assertTrue(base.exists())
        self.assertTrue(shared.exists())
        self.assertEqual(result["outcomes"]["child_live"], 1)
        self.assertEqual(result["outcomes"]["runtime_in_use"], 1)

    def test_removal_resumes_after_directory_was_already_removed(self):
        base, name = lifecycle.allocate("sandbox")
        sid = self.profile(name)
        (base/"marker").write_bytes(b"tracked")
        self.assertTrue(windows.container_present(name, sid))
        with patch.object(safe_executor, "_delete_profile", return_value=0):
            self.assertEqual(lifecycle.retire_owned(base)[0], "retry")
        self.assertFalse(base.exists())
        self.assertTrue(windows.container_present(name, sid))
        self.assertEqual(lifecycle._read(lifecycle._record(base))["state"], "retired")
        report = lifecycle.recover(self.data)
        self.assertEqual(report["logicalBytes"], 7)
        self.assertFalse(windows.container_present(name, sid))
        self.assertFalse(lifecycle._record(base).exists())

    def test_retries_an_abandoned_busy_journal_after_its_directory_is_gone(self):
        directory = self.data/"runs"/"workbench"/"verification-leases"
        base, name = lifecycle.allocate("lease", directory)
        sid = self.profile(name)
        lifecycle._edit(base, pid=2**31-1, identity="exited")
        journal = directory/(base.name+".json")
        atomic_json(journal, dict(base=str(base), name=name, busy=True, pid=2**31-1,
                                  identity="exited", childPid=None))
        with patch.object(safe_executor, "_delete_profile", return_value=0):
            self.assertEqual(lifecycle.reclaim_journal(journal)[0], "retry")
        self.assertFalse(base.exists())
        self.assertTrue(windows.container_present(name, sid))
        self.assertTrue(journal.with_suffix(".retired").is_file())
        self.assertEqual(lifecycle.reclaim_journal(journal.with_suffix(".retired"))[0], "removed")
        self.assertFalse(windows.container_present(name, sid))


    def test_legacy_retirement_requires_directory_acl(self):
        directory = self.data/"runs"/"workbench"/"verification-leases"
        directory.mkdir(parents=True)
        name = "Harness.Task." + uuid.uuid4().hex
        sid = self.profile(name)
        base = self.temp/"harness-task-pool-retired"
        base.mkdir()
        journal = directory/(base.name+".retired")
        atomic_json(journal, dict(base=str(base), name=name, busy=False, pid=0, childPid=None))
        self.assertEqual(lifecycle.reclaim_journal(journal)[0], "legacy_acl_unverified")
        self.assertTrue(base.exists())
        self.assertTrue(windows.container_present(name, sid))
        safe_executor._acl(base, sid, "RX")
        self.assertEqual(lifecycle.reclaim_journal(journal)[0], "removed")
        self.assertFalse(journal.exists())
        self.assertFalse(base.exists())
        self.assertFalse(windows.container_present(name, sid))

    def test_reparse_target_and_unindexed_resources_are_not_reclaimed(self):
        target = self.root/"outside"
        target.mkdir()
        content = target/"user-file"
        content.write_text("keep", encoding="utf-8")
        base, _ = lifecycle.allocate("runtime")
        base.rmdir()
        subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(base), str(target)],
                       check=True, capture_output=True)
        try:
            self.assertEqual(lifecycle.retire_owned(base)[0], "unsafe_path")
            self.assertEqual(content.read_text(encoding="utf-8"), "keep")
        finally:
            base.rmdir()
        unknown = self.temp/"harness-task-runtime-untagged"
        unknown.mkdir()
        result = lifecycle.recover(self.data)
        self.assertTrue(unknown.is_dir())
        self.assertIn(str(unknown), [item["path"] for item in result["unverified"]])

    def test_parallel_recovery_never_reclaims_twice_or_blocks_independent_reservation(self):
        stale, _ = lifecycle.allocate("runtime")
        lifecycle._edit(stale, pid=2**31-1, identity="exited")
        deleting, release = threading.Event(), threading.Event()
        original = safe_executor._remove

        def wait_in_removal(base, **kwargs):
            deleting.set()
            self.assertTrue(release.wait(8))
            return original(base, **kwargs)

        record = lifecycle._record(stale)
        with (patch.object(safe_executor, "_remove", side_effect=wait_in_removal) as remove,
              ThreadPoolExecutor(max_workers=2) as workers):
            sweep = workers.submit(lifecycle._reclaim, self.index, record)
            try:
                self.assertTrue(deleting.wait(3))
                second = workers.submit(lifecycle._reclaim, self.index, record)
                self.assertEqual(second.result(2)[0], "locked")
                new = workers.submit(lifecycle.allocate, "runtime")
                self.assertTrue(new.result(2)[0].is_dir())
            finally:
                release.set()
            self.assertEqual(sweep.result(4)[0], "removed")
        self.assertEqual(remove.call_count, 1)


if __name__ == "__main__":
    unittest.main()
