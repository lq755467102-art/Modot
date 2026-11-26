"""Projectless conversation behavior through the authenticated API route seam."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from loop import workbench as W


class ConversationFixture:
    def setUp(self):
        test_root = (
            Path(__file__).resolve().parents[2] / "acceptance" / "_conversation-tests"
        )
        test_root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=test_root)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        p = patch.object(W.P, "data_root", return_value=self.root)
        p.start()
        self.addCleanup(p.stop)
        p = patch.dict(
            "os.environ",
            {
                "HARNESS_HOOKS": str(self.root / "no-user-hooks.json"),
                "HARNESS_HABITS": str(self.root / "owned-habits.md"),
                "HARNESS_HABITS_INDEX": str(self.root / "owned-habits.json"),
                "HARNESS_BASELINE_HITS": str(self.root / "baseline-hits.jsonl"),
            },
        )
        p.start()
        self.addCleanup(p.stop)
        from loop import charter
        p = patch.object(charter, "ROOT", self.root)
        p.start()
        self.addCleanup(p.stop)
        root = self.root

        class Server:
            APP = root
            POLICY = "trusted"

            def _start(self, text, cwd, blank, *, reserved_sid=None):
                ident = reserved_sid or "session-" + str(len(list(root.glob("session-*"))))
                (root / ident).mkdir(exist_ok=reserved_sid is not None)
                (root / ident / "cwd.txt").write_text(cwd, encoding="utf-8")
                return 200, {"session": ident}

            def _resolve_sid(self, ident):
                p = root / ident
                return p if p.is_dir() else None

            def _continue(self, directory, text, **kwargs):
                return 200, {"text": text, "policy": kwargs["policy"]}

        self.sv = Server()

    def call(self, method, suffix="", body=None):
        return W.handle(
            method, "/api/v2/conversations/threads" + suffix, {}, body or {}, self.sv
        )


class ConversationTests(ConversationFixture, unittest.TestCase):
    def test_workroom_bootstrap_lists_workspaces_without_scanning_git(self):
        workspace = W.register(str(self.root))
        with patch.object(W, "git_status", side_effect=AssertionError("startup listed Git changes")):
            code, boot = W.handle("GET", "/api/v2/bootstrap", {"includeGit": ["0"]}, {}, self.sv)
        self.assertEqual(code, 200)
        self.assertEqual(len(boot["workspaces"]), 1)
        summary = boot["workspaces"][0]
        self.assertEqual(summary["id"], workspace["id"])
        self.assertTrue(summary["capabilities"]["files"])
        self.assertNotIn("git", summary)
        self.assertIn("sandboxMode", boot["permissions"])
        self.assertIn("terminal", boot["hostTransportBlocks"])

        git = dict(available=False, files=[], revision=None)
        with patch.object(W, "git_status", return_value=git) as status:
            code, full = W.handle("GET", "/api/v2/bootstrap", {}, {}, self.sv)
        status.assert_called_once_with(workspace)
        self.assertEqual((code, full["workspaces"][0]["git"]), (200, git))

    def test_send_publishes_scope_while_registry_writer_is_busy(self):
        from concurrent.futures import ThreadPoolExecutor
        from bin import server
        from loop import projects
        from loop.filelock import FileLock

        _, first = self.call("POST")
        _, second = self.call("POST")

        def send(item):
            return self.call("POST", "/" + item["id"] + "/send", {"text": "continue check"})

        with (patch.object(self.sv, "_continue", server._continue),
              patch.object(server, "_active_proc", return_value=None),
              patch.object(server, "_continue_unlocked", return_value=(200, {"ok": True})),
              ThreadPoolExecutor(max_workers=2) as calls):
            with FileLock(W.home() / "registry.lock", timeout=0):
                sends = [calls.submit(send, item) for item in (first, second)]
                for sent in sends:
                    self.assertEqual(sent.result(3), (200, {"ok": True}))
                for item in (first, second):
                    self.assertEqual(projects.scope(item["id"])["lastRun"]["resources"], [])

    def test_scope_snapshot_upgrades_inline_records_and_fails_closed(self):
        from loop import execution_policy, projects

        _, item = self.call("POST")
        ident = item["id"]
        legacy = {"at": 1, "resources": []}
        W.change_registry(lambda data: data["threads"][ident].update(resourceSnapshot=legacy))
        self.assertEqual(projects.scope(ident)["lastRun"], legacy)
        current = projects.snapshot(ident)
        self.assertEqual(projects.scope(ident)["lastRun"], current)
        thread = W.registry()["threads"][ident]
        self.assertEqual(thread["resourceSnapshot"], legacy)
        root = W.workspace(thread["workspaceId"])["path"]
        self.assertEqual(execution_policy.binding(root)["resourceSnapshot"], current)

        W.atomic_json(projects._snapshot_path(ident), dict(conversationId="wrong", snapshot=current))
        with self.assertRaises(W.Problem) as unreadable:
            projects.scope(ident)
        self.assertEqual(unreadable.exception.code, "snapshot_unavailable")

    def test_waiting_launch_does_not_block_another_conversation(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        from bin import server
        from loop.filelock import FileLock

        _, first = self.call("POST")
        _, other = self.call("POST")
        gate = self.sv._resolve_sid(first["id"]) / "launch.lock"
        waiting = Event()
        acquire = FileLock.acquire

        def observe(lock):
            if lock.path == gate and lock.timeout > 0:
                waiting.set()
            return acquire(lock)

        def send(item):
            return self.call("POST", "/" + item["id"] + "/send", {"text": "continue check"})

        with (ThreadPoolExecutor(max_workers=2) as calls,
              patch.object(self.sv, "_continue", server._continue),
              patch.object(server, "_active_proc", return_value=object()),
              patch.object(FileLock, "acquire", observe)):
            with FileLock(gate, timeout=0):
                pending = calls.submit(send, first)
                self.assertTrue(waiting.wait(5))
                independent = calls.submit(send, other)
                self.assertEqual(independent.result(3), (409, {"error": "当前运行尚未结束或状态待核对"}))
                self.assertFalse(pending.done())
            self.assertEqual(pending.result(5), independent.result())

    def test_running_conversation_accepts_step_boundary_interjection(self):
        from loop import control

        _, thread = self.call("POST", body={"title": "运行中插话"})
        directory = self.sv._resolve_sid(thread["id"])
        queued = []
        with patch.object(W.RT, "status", return_value={"active": True, "state": "running"}), \
             patch.object(self.sv, "_active_proc", return_value=object(), create=True), \
             patch.object(self.sv, "_queue_pending", side_effect=lambda *args: queued.append(args) or True, create=True), \
             patch.object(control, "request_stop") as stop:
            code, result = self.call("POST", "/" + thread["id"] + "/send",
                                     {"text": "请先收完当前调用，再按新要求继续"})
        self.assertEqual(code, 200, result)
        self.assertTrue(result["queued"])
        self.assertEqual(queued[0][0], directory)
        self.assertEqual(queued[0][2], "interrupt")
        self.assertEqual(queued[0][3]["policy"], "readonly")
        self.assertEqual(queued[0][3]["execution_config"]["crew"], "l0")
        stop.assert_called_once_with(directory, why="工作台用户调整要求", after_turn=True)

    def test_running_l4_draft_uses_the_same_send_route(self):
        from loop import control, execution_policy

        _, thread = self.call("POST", body={"title": "持续委托草案"})
        W.change_registry(lambda data: data["threads"][thread["id"]].update(
            execution=dict(execution_policy.defaults(), crew="l4")))
        queued = []
        with patch.object(W.RT, "status", return_value={"active": True, "state": "running"}), \
             patch.object(self.sv, "_active_proc", return_value=object(), create=True), \
             patch.object(self.sv, "_queue_pending", side_effect=lambda *args: queued.append(args) or True, create=True), \
             patch.object(control, "request_stop"):
            code, result = self.call("POST", "/" + thread["id"] + "/send",
                                     {"text": "补充草案要求", "purpose": "delegation_draft"})
        self.assertEqual(code, 200, result)
        self.assertEqual(queued[0][2], "interrupt")
        self.assertEqual(queued[0][3]["execution_config"]["purpose"], "delegation_draft")
        self.assertEqual(queued[0][3]["execution_config"]["crew"], "l0")

    def test_create_without_workspace_and_resume_readonly(self):
        code, item = self.call(
            "POST", body={"title": "独立讨论", "path": str(self.root.parent)}
        )
        self.assertEqual(code, 200)
        self.assertIsNone(item["workspaceId"])
        self.assertIsNone(item["projectId"])
        code, listing = self.call("GET")
        self.assertEqual(listing["threads"][0]["id"], item["id"])
        code, result = self.call(
            "POST", "/" + item["id"] + "/send", {"text": "你好", "policy": "trusted"}
        )
        self.assertEqual((code, result["policy"]), (200, "readonly"))
        record = W.registry()["threads"][item["id"]]
        directory = Path(W.workspace(record["workspaceId"])["path"])
        self.assertTrue(directory.is_relative_to(W.home() / "conversations"))
        self.assertEqual(
            W.guard_continuation(self.root / item["id"], "trusted", self.sv), "readonly"
        )
        code, boot = W.handle("GET", "/api/v2/bootstrap", {}, {}, self.sv)
        self.assertEqual(boot["workspaces"], [])

    def test_restored_project_picker_associates_without_granting_directories(self):
        from loop import projects
        _,project=W.handle('POST','/api/v2/projects',{},dict(name='Picker project'),self.sv)
        code,item=self.call('POST',body=dict(title='Project chat',projectId=project['id']))
        self.assertEqual(code,200);self.assertEqual(item['projectId'],project['id'])
        self.assertEqual(projects.scope(item['id'])['resources'],[])
        before=len(W.registry()['threads'])
        self.assertEqual(self.call('POST',body=dict(projectId='nonexistent'))[0],404)
        self.assertEqual(len(W.registry()['threads']),before)

    def test_cannot_route_other_workspace_or_engineering_operations(self):
        w = W.register(str(self.root))
        W.change_registry(
            lambda r: r["threads"].update(
                other={"id": "other", "workspaceId": w["id"], "kind": "discussion"}
            )
        )
        self.assertEqual(self.call("POST", "/other/send", {"text": "x"})[0], 404)
        code, item = self.call("POST")
        self.assertEqual(self.call("POST", "/" + item["id"] + "/accept", {})[0], 404)
        record = W.registry()["threads"][item["id"]]
        path = "/api/v2/workspaces/" + record["workspaceId"] + "/file"
        self.assertEqual(
            W.handle("POST", path, {}, {"path": "x", "text": "bad"}, self.sv)[0], 403
        )

    def test_project_resource_requires_grant_selection_and_matching_project(self):
        def request(method, path, body=None):
            return W.handle(method, "/api/v2/" + path, {}, body or {}, self.sv)

        _, project = request("POST", "projects", {"name": "A"})
        _, other = request("POST", "projects", {"name": "B"})
        directory = self.root / "documents"
        directory.mkdir()
        (directory / "note.txt").write_text("project A only", encoding="utf-8")
        _, resource = request(
            "POST",
            "projects/" + project["id"] + "/resources",
            {"path": str(directory), "kind": "documents"},
        )
        _, conversation = self.call("POST")
        scope = "/" + conversation["id"] + "/scope"
        self.assertEqual(
            self.call(
                "POST",
                scope,
                {"projectId": project["id"], "resourceIds": [resource["id"]]},
            )[0],
            403,
        )
        self.assertEqual(
            request(
                "POST",
                "projects/"
                + project["id"]
                + "/resources/"
                + resource["id"]
                + "/permission",
                {"permission": "read"},
            )[0],
            400,
        )
        self.assertEqual(
            request(
                "POST",
                "projects/"
                + project["id"]
                + "/resources/"
                + resource["id"]
                + "/permission",
                {"permission": "read", "confirm": True},
            )[0],
            200,
        )
        self.assertEqual(
            self.call(
                "POST",
                scope,
                {"projectId": other["id"], "resourceIds": [resource["id"]]},
            )[0],
            403,
        )
        self.assertEqual(
            self.call(
                "POST",
                scope,
                {"projectId": project["id"], "resourceIds": [resource["id"]]},
            )[0],
            200,
        )
        from loop import projects

        value = projects.read_resource(conversation["id"], resource["id"], "note.txt")
        self.assertEqual(value["text"], "project A only")
        projects.snapshot(conversation["id"])
        revision = projects.scope(conversation["id"])["revision"]
        self.assertEqual(
            self.call(
                "POST",
                "/" + conversation["id"] + "/send",
                {"text": "x", "expectedScopeRevision": "outdated"},
            )[0],
            409,
        )
        self.call("POST", scope, {"projectId": project["id"], "resourceIds": []})
        self.assertNotEqual(revision, projects.scope(conversation["id"])["revision"])
        with self.assertRaises(W.Problem):
            projects.read_resource(conversation["id"], resource["id"], "note.txt")
        self.call(
            "POST", scope, {"projectId": project["id"], "resourceIds": [resource["id"]]}
        )
        text = projects.prepare_turn(self.root / conversation["id"], "下一轮")
        self.assertIn(resource["id"], text)
        record = W.registry()["threads"][conversation["id"]]
        from loop.tools import readonly_registry

        tools = readonly_registry(Path(W.workspace(record["workspaceId"])["path"]))
        read = tools.get("read_project_resource")
        self.assertEqual(
            set(tools.names()),
            {
                "read_project_resource",
                "read_project_activity",
                "list_project_conversations",
                "read_shared_conversation",
                "ask_human",
                "inspect_subtasks",
                "read_subtask_integration_file",
                "read_discussion_background",
                "read_correction_evidence",
                "inspect_delivery",
                "prepare_file_restoration",
                "read_artifact",
                "inspect_code",
                "search_project_code",
                "read_check_output",
                "inspect_application",
                "inspect_python_debug",
                "read_context_history",
                "read_conversation_dialogue",
                "search_conversation_history",
                "find_github_repositories",
                "read_memory_tree",
                "read_action_evidence",
                "read_code_history",
                "list_plugins",
                "list_plugin_catalog",
                "list_installed_plugins",
                "preview_local_plugin",
                "read_plugin_skill",
                "read_plugin_file",
                "inspect_plugin_tools",
            },
        )
        entries = read({"resource_id": resource["id"], "path": "."})["entries"]
        self.assertEqual([e["name"] for e in entries], ["note.txt"])
        self.assertIsNotNone(read)
        self.assertEqual(
            read({"resource_id": resource["id"], "path": "note.txt"})["text"],
            "project A only",
        )
        request(
            "POST",
            "projects/"
            + project["id"]
            + "/resources/"
            + resource["id"]
            + "/permission",
            {"permission": "none"},
        )
        self.assertFalse(
            read({"resource_id": resource["id"], "path": "note.txt"})["ok"]
        )
        request(
            "POST",
            "projects/"
            + project["id"]
            + "/resources/"
            + resource["id"]
            + "/permission",
            {"permission": "read", "confirm": True},
        )
        with self.assertRaises(W.Problem):
            projects.read_resource(conversation["id"], resource["id"], "../note.txt")
        self.call("POST", scope, {"projectId": None, "resourceIds": []})
        with self.assertRaises(W.Problem):
            projects.read_resource(conversation["id"], resource["id"], "note.txt")
        self.assertTrue(directory.is_dir())


if __name__ == "__main__":
    unittest.main()
