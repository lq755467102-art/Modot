"""Check project import against the current checkout grant before publication."""
import os
import unittest
from unittest.mock import patch

from loop import permissions, projects, repository_import as R, work_folders, workbench as W
from tools.test_conversations import ConversationFixture


@unittest.skipUnless(os.name == "nt", "Windows repository import")
class RepositoryImportTests(ConversationFixture, unittest.TestCase):
    def selected_folder(self):
        root = self.root / "selected"
        root.mkdir()
        return work_folders.configure(str(root))

    def project_chat(self, *, writable=True):
        root = self.root / "Current project"
        root.mkdir()
        owner = projects.open_folder(str(root), True, owned=writable)
        sid = self.call("POST", body={"projectId": owner["id"]})[1]["id"]
        projects.snapshot(sid)
        return root, owner, sid

    def test_project_without_run_write_grant_does_not_fall_back_and_revocation_stops_publication(self):
        root, owner, sid = self.project_chat(writable=False)
        self.selected_folder()
        url = "git@fixture.example:team/sample.git"
        def set_read(permission):
            route = f"/api/v2/projects/{owner['id']}/resources/{owner['folderResourceId']}/permission"
            status, result = W.handle("POST", route, {}, dict(permission=permission, confirm=True), self.sv)
            self.assertEqual(status, 200, result)
        set_read("none")
        def revoke(action, *, human_only, validate_live):
            set_read("none")
            validate_live()
        with patch("loop.tools.current_call", return_value=dict(sessionId=sid, nativeExecutor=True, role="executor")), \
                patch.object(permissions, "authorize", side_effect=revoke) as approvals, patch.object(R, "_download") as download:
            self.assertEqual(R.private_tool(url).get("code"), "project_import_permission")
            set_read("read")
            self.assertEqual(R.private_tool(url).get("code"), "project_import_permission")
            approvals.assert_not_called()
            projects.snapshot(sid)
            self.assertFalse(R.private_tool(url)["ok"])
            approvals.assert_called_once()
            download.assert_not_called()
        self.assertEqual(list(root.iterdir()), [])
