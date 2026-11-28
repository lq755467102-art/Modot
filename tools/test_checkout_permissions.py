"""Project checkout access follows the conversation preset, including persisted projects."""
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from bin import server
from loop import code_service, permissions, projects, workbench as W
from loop.tools import reset_call, set_call
from tools.test_conversations import ConversationFixture


class CheckoutPermissions(ConversationFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.folder = self.root / "existing-project"
        self.folder.mkdir()
        (self.folder / "existing.txt").write_text("original", encoding="utf8")
        status, project = W.handle("POST", "/api/v2/projects", {}, {"path": str(self.folder), "confirm": True}, self.sv)
        self.assertEqual(status, 200, project)
        self.pid = project["id"]
        self.rid = project["folderResourceId"]
        self.assertFalse(project["resources"][self.rid]["write"])
        self.assertFalse(project["resources"][self.rid]["execute"])

    def chat(self, mode="ask"):
        status, thread = self.call("POST", body=dict(projectId=self.pid, permissionMode=mode, confirmFullAccess=mode == "full"))
        self.assertEqual(status, 200, thread)
        return thread["id"]

    def start(self, ident):
        def launched(directory, text, min_reads, min_chars, max_turns, images, ctx, crew, policy, execution_config, **kwargs):
            return 200, {"policy": policy}
        with (patch.object(self.sv, "_continue", server._continue),
              patch.object(server, "_active_proc", return_value=None),
              patch.object(server, "_continue_unlocked", side_effect=launched)):
            status, result = self.call("POST", "/" + ident + "/send", {"text": "Inspect the selected project."})
        self.assertEqual(status, 200, result)
        return result["policy"]

    @contextmanager
    def owner(self, ident):
        run = uuid4().hex
        permissions.freeze(run, ident)
        token = set_call(dict(sessionId=ident, runId=run, actionId=uuid4().hex, nativeExecutor=True, role="executor"))
        try:
            yield W.workspace(projects.conversation(ident)["workspaceId"])["path"]
        finally:
            reset_call(token)

    def grant(self, rid, permission, **flags):
        route = f"/api/v2/projects/{self.pid}/resources/{rid}/permission"
        status, result = W.handle("POST", route, {}, dict(permission=permission, confirm=True, **flags), self.sv)
        self.assertEqual(status, 200, result)

    def test_open_existing_project_in_each_preset(self):
        for mode in ("ask", "auto", "full"):
            with self.subTest(mode=mode):
                ident = self.chat(mode)
                self.assertEqual(self.start(ident), "workspace")
                with self.owner(ident) as root:
                    for capability in (None, "write", "execute"):
                        code_service.context_resource(root, self.rid, capability)
                    result = code_service.invoke(root, "write", self.rid, relative_path=mode + ".md", create=True, text=mode)
                    self.assertTrue(result["ok"], result)
                    self.assertTrue(result['staged'])
                    self.assertFalse((self.folder / (mode + '.md')).exists())
                    page = code_service.invoke(root, 'inspect', self.rid, relative_path=mode + '.md')
                    self.assertEqual(page.get('text'), mode, page)
                    from loop import project_task_view
                    from loop.tools import current_call
                    project_task_view.settle(ident, current_call()['runId'], 'paused')
                self.assertIn(str(self.folder), code_service.selected_write_paths(ident))
        stored = projects.project(self.pid)["resources"][self.rid]
        self.assertFalse(stored["write"])
        self.assertFalse(stored["execute"])

    def test_revocation_and_regrant_do_not_reactivate_frozen_checkout(self):
        ident = self.chat("full")
        self.start(ident)
        with self.owner(ident) as root:
            code_service.context_resource(root, self.rid, "execute")
            self.grant(self.rid, "none")
            for capability in (None, "write", "execute"):
                with self.assertRaises(W.Problem):
                    code_service.context_resource(root, self.rid, capability)
            self.grant(self.rid, "read")
            for capability in (None, "write", "execute"):
                with self.assertRaises(W.Problem):
                    code_service.context_resource(root, self.rid, capability)
            self.assertNotIn(self.rid, projects.history_read_resources(ident))
        self.assertEqual(self.start(ident), "workspace")
        with self.owner(ident) as root:
            code_service.context_resource(root, self.rid, "write")

    def test_only_selected_checkout_inherits_and_additional_directory_keeps_explicit_grants(self):
        ident = self.chat()
        extra = self.root / "additional"
        extra.mkdir()
        status, linked = W.handle("POST", f"/api/v2/projects/{self.pid}/resources", {},
                                  dict(path=str(extra), kind="code"), self.sv)
        self.assertEqual(status, 200, linked)
        other = linked["id"]
        self.grant(other, "read", execute=True)
        checkout = self.root / "checkout"
        checkout.mkdir()
        binding = dict(id="active-worktree", projectId=self.pid, sourcePath=str(self.folder),
                       path=str(checkout), active=True, used=True)
        W.change_registry(lambda db: db["threads"][ident].update(codeWorktrees={self.rid: binding}))
        projects.set_scope(ident, dict(projectId=self.pid, resourceIds=[self.rid, other]), self.sv)
        self.assertEqual(self.start(ident), "workspace")
        with self.owner(ident) as root:
            for capability in ("write", "execute"):
                _, target, _, _ = code_service.context_resource(root, binding["id"], capability)
                self.assertEqual(Path(target["path"]), checkout)
                with self.assertRaises(W.Problem):
                    code_service.context_resource(root, self.rid, capability)
            code_service.context_resource(root, other, "execute")
            with self.assertRaises(W.Problem):
                code_service.context_resource(root, other, "write")
            self.grant(self.rid, "none")
            with self.assertRaises(W.Problem):
                code_service.context_resource(root, binding["id"], "execute")
            code_service.context_resource(root, other, "execute")

    def test_old_snapshot_only_acquires_access_on_a_new_run(self):
        ident = self.chat()
        old = {"projectId": self.pid, "resources": [dict(id=self.rid, path=str(self.folder), permission="read",
                                                          write=False, execute=False)]}
        W.change_registry(lambda db: db["threads"][ident].update(resourceSnapshot=old))
        with self.owner(ident) as root:
            with self.assertRaises(W.Problem):
                code_service.context_resource(root, self.rid, "write")
        self.assertEqual(self.start(ident), "workspace")
        with self.owner(ident) as root:
            code_service.context_resource(root, self.rid, "write")

    def test_stale_checkbox_request_does_not_change_the_existing_grant(self):
        ident = self.chat()
        self.start(ident)
        recorded = projects.scope(ident)["checkoutAccess"]
        self.grant(self.rid, "read", write=True, execute=True)
        stored = projects.project(self.pid)["resources"][self.rid]
        self.assertFalse(stored["write"])
        self.assertFalse(stored["execute"])
        self.assertEqual(projects.scope(ident)["checkoutAccess"], recorded)
        with self.owner(ident) as root:
            code_service.context_resource(root, self.rid, "write")

    def test_archiving_keeps_authorized_history_readable_without_inheriting_writes(self):
        ident = self.chat()
        self.start(ident)
        W.change_registry(lambda db: db["threads"][ident].update(archived=True))
        self.assertIn(self.rid, projects.history_read_resources(ident))
        with self.owner(ident) as root:
            code_service.context_resource(root, self.rid)
            with self.assertRaises(W.Problem):
                code_service.context_resource(root, self.rid, "write")

    def test_no_inherited_access_in_readonly_or_delegated_scope(self):
        ident = self.chat("full")
        for flag in ("readonlyEvidence", "subtaskOf", "scheduledPlanId", "archived", "activeDelegation"):
            with self.subTest(flag=flag):
                thread = dict(projects.conversation(ident), **{flag: True})
                self.assertFalse(projects._scope(thread).get("checkoutAccess"))
        projects.set_scope(ident, dict(projectId=self.pid, resourceIds=[]), self.sv)
        self.assertEqual(self.start(ident), "readonly")

    def test_active_run_reserves_inherited_checkout(self):
        ident = self.chat()
        self.start(ident)
        with patch.object(W.RT, "status", return_value={"active": True}):
            with self.assertRaises(W.Problem) as blocked:
                W.guard_workspace_writer(self.folder, self.sv)
        self.assertEqual(blocked.exception.code, "writer_busy")
