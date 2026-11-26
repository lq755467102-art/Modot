"""Exercise confirmed archived-chat deletion through the authenticated API."""

from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

from loop import workbench as W
from tools.test_conversations import ConversationFixture


class ArchiveDeletionTests(ConversationFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        runs = self.root / "runs"
        def start(text, cwd, blank, *, reserved_sid=None):
            name = reserved_sid or "session-" + str(len(list(runs.glob("session-*"))))
            directory = runs / name
            directory.mkdir(exist_ok=reserved_sid is not None)
            (directory / "cwd.txt").write_text(cwd, encoding="utf-8")
            return 200, {"session": name}

        def resolve(name):
            directory = runs / name
            return directory if directory.is_dir() else None

        self.sv._start = start
        self.sv._resolve_sid = resolve

    def create(self):
        code, item = self.call("POST")
        self.assertEqual(code, 200, item)
        return item["id"]

    def delete(self, ident, *, confirm=True):
        return self.call("POST", "/" + ident + "/delete", {"confirm": confirm})

    def archive(self, ident, archived=True):
        return self.call("POST", "/" + ident + "/archive", {"archived": archived})

    def test_only_confirmed_archived_conversation_is_removed(self):
        ident, other = self.create(), self.create()
        thread = W.registry()["threads"][ident]
        working = self.root / "my-project"
        working.mkdir()
        source = working / "README.md"
        source.write_text("working copy", encoding="utf-8")
        W.change_registry(lambda data: data["threads"][ident].update(
            workResource={"path": str(working), "permission": "read"}
        ))
        private = Path(W.workspace(thread["workspaceId"])["path"])
        imported = W.home() / "attachments" / ident / "sample"
        imported.mkdir(parents=True)
        (imported / "data.txt").write_text("attachment copy", encoding="utf-8")
        run = self.sv._resolve_sid(ident)
        self.assertEqual(self.delete(ident)[1].get("code"), "archive_required")
        self.assertEqual(self.archive(ident)[0], 200)
        self.assertEqual(self.delete(ident, confirm=1)[1].get("code"), "confirmation")
        self.assertEqual(self.delete(ident), (200, {"ok": True, "deleted": ident}))
        self.assertEqual(self.delete(ident)[0], 404)
        self.assertEqual([row["id"] for row in self.call("GET")[1]["threads"]], [other])
        self.assertNotIn(thread["workspaceId"], W.registry()["workspaces"])
        self.assertFalse(run.exists())
        self.assertFalse(private.exists())
        self.assertFalse(imported.exists())
        self.assertEqual(source.read_text(encoding="utf-8"), "working copy")

    def test_running_and_scheduled_conversations_are_protected(self):
        ident = self.create()
        self.assertEqual(self.archive(ident)[0], 200)
        with patch.object(W.RT, "status", return_value={"active": True}):
            self.assertEqual(self.delete(ident)[1].get("code"), "active_run")
        W.change_registry(lambda data: data["threads"][ident].update(scheduledPlanId="plan"))
        self.assertEqual(self.delete(ident)[1].get("code"), "schedule_history_managed")
        self.assertTrue(self.sv._resolve_sid(ident).exists())

    def test_failed_cleanup_is_visible_and_retry_does_not_restore_partial_files(self):
        ident = self.create()
        self.assertEqual(self.archive(ident)[0], 200)
        run = self.sv._resolve_sid(ident)
        remove = shutil.rmtree

        def deny_workspace_removal(path):
            if path != run:
                raise PermissionError("sharing violation")
            remove(path)

        with patch("shutil.rmtree", side_effect=deny_workspace_removal):
            self.assertEqual(self.delete(ident)[1].get("code"), "archive_cleanup_pending")
        self.assertFalse(run.exists())
        self.assertTrue(self.call("GET")[1]["threads"][0]["deletionPending"])
        self.assertEqual(self.archive(ident, False)[1].get("code"), "archive_cleanup_pending")
        self.assertEqual(self.delete(ident)[0], 200)
        self.assertEqual(self.call("GET")[1]["threads"], [])

    def test_registered_private_directory_must_be_owned(self):
        ident = self.create()
        self.assertEqual(self.archive(ident)[0], 200)
        workspace_id = W.registry()["threads"][ident]["workspaceId"]
        unrelated = self.root / "unrelated"
        unrelated.mkdir()
        W.change_registry(lambda data: data["workspaces"][workspace_id].update(path=str(unrelated)))
        self.assertEqual(self.delete(ident)[1].get("code"), "invalid_path")
        self.assertTrue(unrelated.is_dir())
