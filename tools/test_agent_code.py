"""Agent code work reuses real filesystem, Git and process services."""

import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch
from tools.test_conversations import ConversationFixture
from loop import workbench as W, projects, loop as L
from loop.tools import default_registry
from loop.provider import MockProvider
from loop.session import Session
from loop.policy import Policy


class AgentCodeTests(ConversationFixture, unittest.TestCase):
    def test_process_death_after_write_recovers_observation_without_replaying(self):
        import os
        import subprocess
        from loop import change_history, code_service as C

        root = self.setup_code()
        W.safe_git(self.ws, ["switch", "-c", "agent/crash-write"])
        file = root / "calc.py"
        revision = W.digest(file.read_bytes())
        script = """
import os, sys
from loop import code_service as C, workbench as W
from loop.tools import set_call
set_call(dict(sessionId=sys.argv[1], runId='a'*32, actionId='b'*32, role='executor'))
original = W.save_bytes
def stop_after_effect(*args, **kwargs):
    original(*args, **kwargs)
    os._exit(74)
W.save_bytes = stop_after_effect
result = C.invoke(sys.argv[2], 'edit', sys.argv[3], relative_path='calc.py',
                  expected_revision=sys.argv[4], old_text='return a-b', new_text='return a+b')
raise RuntimeError(result)
"""
        child = subprocess.run(
            [sys.executable, "-c", script, self.t["id"], str(self.execution_root), self.r["id"], revision],
            env=dict(os.environ, HARNESS_DATA=str(self.root)), capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(child.returncode, 74, child.stderr)
        self.assertIn(b"return a+b", file.read_bytes())
        with file.open("ab") as stream:
            stream.write(b"# human edit after process exit\n")
        expected = file.read_bytes()
        with W.FileLock(W.writer_lock_path(root)):
            observations = C.reconcile_interrupted_writes(self.t["id"], [root])
            self.assertEqual(len(observations), 1)
            self.assertEqual(C.reconcile_interrupted_writes(self.t["id"], [root]), [])
        operation = C.operation(self.t["id"], observations[0]["operationId"])
        self.assertEqual(operation["state"], "unknown")
        self.assertTrue(operation["result"]["ownerStopped"])
        self.assertEqual(operation["result"]["projectRevision"], W.current_revision(self.ws))
        self.assertEqual(change_history.detail(self.t["id"], operation["id"])["state"], "unknown")
        self.assertEqual(file.read_bytes(), expected)

    def test_pytest_report_flags_reuse_only_the_same_role_and_environment(self):
        from loop import code_service as C

        self.setup_code()
        thread, resource, workspace = self.t["id"], self.r["id"], self.ws["id"]
        source = {"runId": "run", "role": "executor", "actionId": "first"}
        state = {"revision": "candidate", "branch": "agent/test", "head": "head"}
        command = ["python", "-m", "pytest", "tests/test_urls.py", "-q"]
        C.record(thread, {
            "id": "operation", "operation": "check", "resourceId": resource,
            "workspaceId": workspace, "source": source, "beforeRevision": state["revision"],
            "branch": state["branch"], "head": state["head"], "state": "completed",
            "target": {"argv": command, "capture_changes": False, "environment": "runtime-a"},
            "result": {"verification": {"status": "failed"}},
        })

        def check(argv, actor=source, environment="runtime-a", candidate=state):
            return C.check_boundary(thread, resource, workspace, actor, candidate,
                                    argv, 30, environment=environment)

        formatted = [*command, "--tb=no", "-rf"]
        previous, status, _ = check(formatted)
        self.assertEqual((previous["id"], status), ("operation", "failed"))
        self.assertIsNone(check([*formatted, "-k", "narrow"]))
        self.assertIsNone(check(formatted, actor=dict(source, role="reviewer")))
        self.assertIsNone(check(formatted, environment="runtime-b"))
        self.assertIsNone(check(formatted, candidate=dict(state, revision="new")))
        self.assertEqual(C.check_blocker("fixture 'mocker' not found"), "missing_fixture")

        original = C.operation(thread, "operation")
        original["result"]["verification"]["status"] = "passed"
        C.record(thread, dict(original, id="operation-pass"))
        extra = ["python", "-m", "pytest", "tests/test_views.py"]
        self.assertIsNone(C.check_boundary(thread, resource, workspace, source, state, extra, 30,
                                          environment="runtime-a", scope_reason=""))
        reason = "Verify the new error response branch in views"
        self.assertIsNone(C.check_boundary(thread, resource, workspace, source, state, extra, 30,
                                           environment="runtime-a", scope_reason=reason))
        C.record(thread, dict(original, id="extension", target=dict(original["target"], argv=extra, scope_reason=reason)))
        self.assertIsNone(C.check_boundary(thread, resource, workspace, source, state, [*extra, "-k", "errors"], 30,
                                              environment="runtime-a", scope_reason=reason.upper()))

    def test_scoped_search_and_paged_line_reads_do_not_prepare_history(self):
        from loop import change_history, code_service as C, scoped_roles
        from loop.tools import reset_call, set_call

        root = self.setup_code()
        original = b"# line one\r\n" + b"filler\r\n" * 900 + b"needle = 42\r\nlast = 0\r\n"
        (root / "calc.py").write_bytes(original)
        resource = self.r["id"]
        for role in ("planner", "executor", "reviewer"):
            with self.subTest(role=role):
                registry = default_registry(self.execution_root) if role == "executor" else scoped_roles.registry(self.execution_root, role=role)
                self.assertTrue(registry.get("search_project_code").readonly)
                self.assertTrue(registry.get("inspect_code").readonly)
                token = set_call(dict(sessionId=self.t["id"], runId="a" * 32, actionId="b" * 32, role=role))
                try:
                    with patch.object(change_history, "prepare", side_effect=AssertionError("read cannot snapshot")):
                        result = C.invoke(self.execution_root, "search", resource, query="needle")
                        self.assertEqual(result["matches"], [dict(path="calc.py", line=902, text="needle = 42")])
                        line = C.invoke(self.execution_root, "inspect", resource, relative_path="calc.py", line=902, line_count=1)
                        self.assertEqual((line["text"], line["startLine"], line["revision"]), ("needle = 42\r\n", 902, W.digest(original)))
                        first = C.invoke(self.execution_root, "inspect", resource, relative_path="calc.py")
                        if first['nextOffset'] is None:
                            self.assertEqual(first['text'],original.decode('utf8'))
                        else:
                            second = C.invoke(self.execution_root, "inspect", resource, relative_path="calc.py", offset=first["nextOffset"])
                            self.assertEqual(first["text"] + second["text"], original.decode("utf8"))
                            self.assertIsNone(second["nextOffset"])
                        outside = C.invoke(self.execution_root, "search", resource, query="needle", relative_path="../")
                        self.assertFalse(outside["ok"])
                finally:
                    reset_call(token)

    def test_deadline_during_history_preparation_prevents_command_launch(self):
        from loop import change_history, code_service as C
        from loop.tools import reset_call, set_call, set_stop

        self.setup_code()
        W.safe_git(self.ws, ["switch", "-c", "agent/deadline"])
        token = set_call(dict(sessionId=self.t["id"], runId="a" * 32, actionId="b" * 32, role="executor"))
        expired = threading.Event()
        previous = set_stop(expired.is_set)
        prepare = change_history.prepare

        def expire_after_prepare(*args, **kwargs):
            history_id = prepare(*args, **kwargs)
            expired.set()
            return history_id

        try:
            with (patch.object(change_history, "prepare", side_effect=expire_after_prepare),
                  patch.object(W, "launch") as launch):
                result = C.invoke(self.execution_root, "check", self.r["id"],
                                  argv=[sys.executable, "-c", "print('too late')"],
                                  expected_revision=W.current_revision(self.ws), capture_changes=True)
            self.assertEqual(result["code"], "cancelled")
            launch.assert_not_called()
            saved = change_history.detail(self.t["id"], result["changeId"])
            self.assertEqual(saved["state"], "failed")
            self.assertIsNone(saved["after"])
        finally:
            set_stop(previous)
            reset_call(token)

    def test_scoped_edit_rejects_ambiguous_text_and_whole_file_truncation(self):
        from loop import code_service as C
        from loop.tools import reset_call, set_call

        root = self.setup_code()
        W.safe_git(self.ws, ["switch", "-c", "agent/edit"])
        file = root / "calc.py"
        original = ("# preserved\n" * 140 + "def add(a,b):\n    return a-b\n").encode()
        file.write_bytes(original)
        rid = self.r["id"]
        revision = W.digest(original)
        token = set_call(dict(sessionId=self.t["id"],runId="a"*32,actionId="b"*32,role="executor"))
        try:
            common = dict(relative_path="calc.py", expected_revision=revision)
            truncated = C.invoke(self.execution_root, "write", rid, **common, text="placeholder")
            self.assertEqual(truncated.get("code"), "unsafe_replacement")
            ambiguous = C.invoke(self.execution_root, "write", rid, **common, old_text="# preserved", new_text="# changed")
            self.assertEqual(ambiguous.get("code"), "ambiguous_edit")
            result = C.invoke(self.execution_root, "write", rid, **common, old_text="    return a-b", new_text="    return a+b")
            self.assertTrue(result["ok"], result)
            self.assertEqual(file.read_bytes(), original.replace(b"return a-b", b"return a+b"))
        finally:
            reset_call(token)

    @unittest.skipUnless(sys.platform == "win32", "recovery uses exclusive Windows writes")
    def test_own_write_restores_only_same_run_and_unchanged_versions(self):
        from loop import code_service as C
        from loop.tools import reset_call, set_call

        root = self.setup_code()
        W.safe_git(self.ws, ["switch", "-c", "agent/own-undo"])
        file = root / "calc.py"
        original = b"# user edit\r\ndef add(a,b):\r\n    return a-b\r\n"
        file.write_bytes(original)
        rid = self.r["id"]

        def invoke(run, operation, **args):
            token = set_call(dict(sessionId=self.t["id"],runId=run*32,actionId="c"*32,role="executor"))
            try:
                return C.invoke(self.execution_root, operation, rid, **args)
            finally:
                reset_call(token)

        def edit():
            return invoke("a","edit",relative_path="calc.py", expected_revision=W.digest(original),
                          old_text="def add(a,b):\n    return a-b", new_text="def add(a,b):\n    return 0")

        write = edit()
        self.assertTrue(write["ok"], write)
        self.assertEqual(invoke("b","undo",source_operation_id=write["operationId"])["code"],"not_own_latest_write")
        restored = invoke("a","undo",source_operation_id=write["operationId"])
        self.assertTrue(restored["ok"], restored)
        self.assertEqual(restored["authorizationMode"], "own_write")
        self.assertEqual(file.read_bytes(), original)
        write = edit()
        manual = b"# newer human edit\r\n"
        file.write_bytes(manual)
        rejected = invoke("a","undo",source_operation_id=write["operationId"])
        self.assertEqual(rejected["code"],"external_edit")
        self.assertEqual(file.read_bytes(), manual)

    def test_verification_boundary_returns_versioned_receipts(self):
        from loop import code_service as C, permissions
        from loop.tools import reset_call, set_call

        self.setup_code()
        W.safe_git(self.ws, ["switch", "-c", "agent/check-boundary"])
        route = "/" + self.t["id"] + "/permissions"
        _, preset = self.call("GET", route)
        code, preset = self.call("POST", route, dict(mode="full", expectedRevision=preset["revision"], confirm=True))
        self.assertEqual((code, preset["sandboxMode"]), (200, "danger-full-access"))
        permissions.freeze("a"*32,self.t["id"])
        token = set_call(dict(sessionId=self.t["id"],runId="a"*32,actionId="c"*32,role="executor"))
        try:
            revision = W.current_revision(self.ws)
            with (patch.dict("os.environ", HARNESS_DATA=str(self.root), HARNESS_PYTHON=sys.executable),
                  patch.object(W, "launch", wraps=W.launch) as launch):
                def run_check(script):
                    return C.invoke(self.execution_root, "check", self.r["id"],
                                    argv=[sys.executable,"-c",script],expected_revision=revision, capture_changes=True)
                one = run_check("print('verified')")
                self.assertEqual(one.get("verification"),dict(status="passed",revision=revision),one)
                duplicate = run_check("print('verified')")
                self.assertTrue(duplicate['reused'])
                self.assertEqual(duplicate['operationId'],one['operationId'])
                self.assertTrue(run_check("print('different check')")["ok"])
                self.assertTrue(run_check("print('verified')")['reused'])
                for code in range(1,4):
                    failure = run_check(f"raise SystemExit({code})")
                    self.assertEqual(failure["verification"]["status"], "failed", failure)
                self.assertFalse(run_check("raise SystemExit(4)")['ok'])
                self.assertEqual(launch.call_count,6)
                refused = C.invoke(self.execution_root, "check", self.r["id"],
                                   argv=[W.git_exe(), "stash", "push"], expected_revision=revision)
                self.assertEqual(refused.get("code"), "check_changes_git_state")
                self.assertEqual(launch.call_count,6)
                path = Path(self.ws["path"])
                (path / "calc.py").write_text("# new verification revision\n", encoding="utf-8")
                revision = W.current_revision(self.ws)
                stale = run_check("from pathlib import Path; Path('calc.py').write_text('# changed during check')")
                self.assertEqual((path / "calc.py").read_text(encoding="utf-8"), "# changed during check")
                self.assertEqual(stale["verification"]["status"], "stale")
                self.assertEqual(stale["process"]["exitCode"], 0)
                self.assertFalse(stale["ok"])
        finally:
            reset_call(token)

    def setup_code(self):
        _, t = self.call("POST")
        _, p = W.handle("POST", "/api/v2/projects", {}, {"name": "Code"}, self.sv)
        path = self.root / "code"
        path.mkdir()
        (path / "calc.py").write_text(
            "def add(a,b):\n    return a-b\n", encoding="utf-8"
        )
        (path / "check.py").write_text(
            "from calc import add\nassert add(2,3)==5\nprint('fixed check passed')\n",
            encoding="utf-8",
        )
        ws = W.register(str(path))
        for args in (
            ["init", "-b", "master"],
            ["config", "user.name", "Fixture"],
            ["config", "user.email", "fixture@example.invalid"],
            ["add", "."],
            ["commit", "-m", "initial"],
        ):
            W.git(ws, args)
        _, resource = W.handle(
            "POST",
            "/api/v2/projects/" + p["id"] + "/resources",
            {},
            {"path": str(path), "kind": "code"},
            self.sv,
        )
        self.p, self.r, self.t, self.ws = p, resource, t, ws
        self.grant(permission="read", write=True, execute=True, confirm=True)
        projects.set_scope(
            t["id"], {"projectId": p["id"], "resourceIds": [resource["id"]]}, self.sv
        )
        projects.snapshot(t["id"])
        self.execution_root = Path(
            W.workspace(W.registry()["threads"][t["id"]]["workspaceId"])["path"]
        )
        self.session = Session(self.root / t["id"])
        return path

    def grant(self, **body):
        return W.handle(
            "POST",
            "/api/v2/projects/"
            + self.p["id"]
            + "/resources/"
            + self.r["id"]
            + "/permission",
            {},
            body,
            self.sv,
        )

    def run_tool(self, name, args):
        L.run(
            "Fix code and report actual checks",
            MockProvider([{"tool": name, "args": args}, {"text": "done"}]),
            default_registry(self.execution_root),
            lambda _: {"ok": True},
            session=self.session,
            max_turns=2,
            policy=Policy("workspace", root=self.execution_root),
        )
        events = [
            json.loads(line)
            for line in self.session.events_path.read_text(
                encoding="utf-8"
            ).splitlines()
        ]
        return next(e for e in reversed(events) if e["act"] == "tool")

    def test_check_without_sandbox_runtime_records_confirmed_failure(self):
        from loop import code_service

        self.setup_code()
        W.git(self.ws, ["checkout", "-b", "agent/check-preflight"])
        with patch.object(W, "launch", side_effect=W.Problem("sandbox_runtime", "缺少隔离运行时", 503)):
            self.run_tool("run_project_check", {
                "resource_id": self.r["id"],
                "argv": [sys.executable, "check.py"],
                "expected_revision": W.current_revision(self.ws),
            })
        operation = code_service.operations(self.t["id"])["operations"][-1]
        self.assertEqual(operation["state"], "failed", operation["result"])
        self.assertEqual(operation["result"]["code"], "sandbox_runtime")
        self.assertEqual(operation["result"]["error"], "缺少隔离运行时")
        self.assertNotIn("process", operation["result"])
        self.assertNotIn("changeId", operation["result"])

    def test_agent_repairs_real_code_and_records_failed_and_successful_checks(self):
        path = self.setup_code()
        rid = self.r["id"]
        initial = W.current_revision(self.ws)
        self.run_tool(
            "prepare_code_branch",
            {
                "resource_id": rid,
                "branch": "agent/repair",
                "expected_revision": initial,
            },
        )
        self.assertEqual(W.git_status(self.ws)["branch"], "agent/repair")
        self.run_tool(
            "run_project_check",
            {
                "resource_id": rid,
                "argv": [sys.executable, "check.py"],
                "expected_revision": W.current_revision(self.ws),
            },
        )
        operations = self.call("GET", "/" + self.t["id"] + "/code-operations")[1][
            "operations"
        ]
        self.assertEqual(operations[-1]["result"]["process"]["exitCode"], 1)
        old = W.file_read(self.ws, "calc.py")
        self.run_tool(
            "write_project_file",
            {
                "resource_id": rid,
                "relative_path": "calc.py",
                "text": "def add(a,b):\n    return a+b\n",
                "expected_revision": old["revision"],
            },
        )
        self.assertIn("return a+b", (path / "calc.py").read_text())
        self.run_tool(
            "run_project_check",
            {
                "resource_id": rid,
                "argv": [sys.executable, "check.py"],
                "expected_revision": W.current_revision(self.ws),
            },
        )
        operations = self.call("GET", "/" + self.t["id"] + "/code-operations")[1][
            "operations"
        ]
        self.assertEqual(operations[-1]["result"]["process"]["exitCode"], 0)
        self.assertIn("fixed check passed", operations[-1]["result"]["text"])
        self.assertTrue(
            all(op["source"]["actionId"] and op["source"]["runId"] for op in operations)
        )

    def test_check_binding_cannot_overwrite_fast_exit_receipt(self):
        from loop import code_service

        self.setup_code()
        self.run_tool("prepare_code_branch", {
            "resource_id": self.r["id"], "branch": "agent/receipt",
            "expected_revision": W.current_revision(self.ws),
        })
        exited = threading.Event()
        write = W.atomic_json

        def order_writes(path, value):
            is_check = path.parent.name == "processes" and value.get("kind") == "check"
            if is_check and value.get("operationId") and value["state"] == "running" and path.exists():
                # Hold the obsolete startup snapshot until the collector commits the exit.
                stale = dict(value)
                if exited.wait(10):
                    write(path, stale)
                    return
            write(path, value)
            if is_check and value["state"] == "completed":
                exited.set()

        with patch.object(W, "atomic_json", order_writes), patch.object(code_service, "atomic_json", order_writes):
            self.run_tool("run_project_check", {
                "resource_id": self.r["id"],
                "argv": [sys.executable, "-c", "import time;time.sleep(.25);print('fast check')"],
                "expected_revision": W.current_revision(self.ws),
            })
        result = code_service.operations(self.t["id"])["operations"][-1]["result"]
        self.assertEqual(result["process"]["exitCode"], 0)
        self.assertEqual(result["process"]["source"]["sessionId"], self.t["id"])
        self.assertIn("fast check", result["text"])

    def test_protected_branch_paths_versions_and_live_grants_are_enforced(self):
        path = self.setup_code()
        rid = self.r["id"]
        before = (path / "calc.py").read_bytes()
        args = {
            "resource_id": rid,
            "relative_path": "calc.py",
            "text": "bad",
            "expected_revision": W.digest(before),
        }
        self.run_tool("write_project_file", args)
        self.assertEqual((path / "calc.py").read_bytes(), before)
        self.run_tool(
            "prepare_code_branch",
            {
                "resource_id": rid,
                "branch": "agent/guards",
                "expected_revision": W.current_revision(self.ws),
            },
        )
        (path / "calc.py").write_text("human changed\n")
        self.run_tool("write_project_file", args)
        self.assertEqual((path / "calc.py").read_text(), "human changed\n")
        self.run_tool(
            "write_project_file", dict(args, relative_path="../outside.py", create=True)
        )
        self.assertFalse((path.parent / "outside.py").exists())
        self.grant(permission="read", write=False, execute=False, confirm=True)
        self.run_tool(
            "write_project_file",
            dict(args, expected_revision=W.file_read(self.ws, "calc.py")["revision"]),
        )
        self.assertEqual((path / "calc.py").read_text(), "human changed\n")
        self.run_tool(
            "run_project_check",
            {
                "resource_id": rid,
                "argv": [
                    sys.executable,
                    "-c",
                    "open('should-not-exist','w').write('x')",
                ],
                "expected_revision": W.current_revision(self.ws),
            },
        )
        self.assertFalse((path / "should-not-exist").exists())
        projects.snapshot(self.t["id"])
        self.grant(permission="read", write=True, execute=True, confirm=True)
        self.run_tool(
            "write_project_file",
            dict(args, expected_revision=W.file_read(self.ws, "calc.py")["revision"]),
        )
        self.assertEqual((path / "calc.py").read_text(), "human changed\n")

    def test_active_conversation_writer_blocks_legacy_and_agent_operations(self):
        import os
        from loop import runtime as RT

        path = self.setup_code()
        _, other = self.call("POST")
        projects.set_scope(
            other["id"],
            {"projectId": self.p["id"], "resourceIds": [self.r["id"]]},
            self.sv,
        )
        projects.snapshot(other["id"])
        directory = W.P.data_root() / "runs" / other["id"]
        directory.mkdir(parents=True)
        RT.register(directory, os.getpid(), policy="workspace")
        result = self.run_tool(
            "prepare_code_branch",
            {
                "resource_id": self.r["id"],
                "branch": "agent/blocked",
                "expected_revision": W.current_revision(self.ws),
            },
        )
        self.assertFalse(result["ok"])
        self.assertEqual(W.git_status(self.ws)["branch"], "master")
        from loop.code_service import RuntimeView

        with self.assertRaises(W.Problem) as raised:
            W.guard_workspace_writer(path, RuntimeView(), exclude_thread=self.t["id"])
        self.assertEqual(raised.exception.code, "writer_busy")
        with self.assertRaises(W.Problem) as admission:
            W.guard_continuation(self.root / self.t["id"], "workspace", RuntimeView())
        self.assertEqual(admission.exception.code, "writer_busy")

    def test_read_tool_cannot_override_dispatch_and_long_failed_output_can_be_paged(
        self,
    ):
        from loop.tools import set_call, reset_call, readonly_registry
        from loop import code_service

        path = self.setup_code()
        self.run_tool(
            "prepare_code_branch",
            {
                "resource_id": self.r["id"],
                "branch": "agent/logs",
                "expected_revision": W.current_revision(self.ws),
            },
        )
        token = set_call(
            {
                "sessionId": self.t["id"],
                "runId": "fixture",
                "actionId": "read-only",
                "tool": "inspect_code",
            }
        )
        try:
            result = readonly_registry(self.execution_root).get("inspect_code")(
                {
                    "resource_id": self.r["id"],
                    "_op": "write",
                    "relative_path": "calc.py",
                    "text": "bad",
                }
            )
            self.assertFalse(result["ok"])
            self.assertIn("return a-b", (path / "calc.py").read_text())
        finally:
            reset_call(token)
        self.run_tool(
            "run_project_check",
            {
                "resource_id": self.r["id"],
                "argv": [
                    sys.executable,
                    "-c",
                    "import sys;print('a'*70000);print('trailing failure evidence');sys.exit(1)",
                ],
                "expected_revision": W.current_revision(self.ws),
            },
        )
        op = self.call("GET", "/" + self.t["id"] + "/code-operations")[1]["operations"][
            -1
        ]
        value = op["result"]
        output = value["text"]
        while value["hasMore"]:
            code, value = W.handle(
                "GET",
                "/api/v2/conversations/threads/"
                + self.t["id"]
                + "/code-operations/"
                + op["id"]
                + "/output",
                {"after": [str(value["next"])]},
                {},
                self.sv,
            )
            self.assertEqual(code, 200, value)
            output += value["text"]
        self.assertIn("trailing failure evidence", output)
        self.assertEqual(value["process"]["exitCode"], 1)
        _, other = self.call("POST")
        with self.assertRaises(W.Problem):
            code_service.output_page(other["id"], value["process"]["id"])
        self.assertNotIn("codeOperations", W.registry()["threads"][self.t["id"]])

    def test_check_timeout_waits_for_real_exit_and_git_hooks_do_not_bypass_execute_grant(
        self,
    ):
        path = self.setup_code()
        self.grant(permission="read", write=True, execute=False, confirm=True)
        projects.snapshot(self.t["id"])
        hook = path / ".git" / "hooks" / "post-checkout"
        hook.write_text("#!/bin/sh\necho executed > hook-executed\n", encoding="utf-8")
        hook.chmod(0o755)
        filter_hook = path / ".git" / "hooks" / "filter-test"
        filter_hook.write_text(
            "#!/bin/sh\necho executed > filter-executed\ncat\n", encoding="utf-8"
        )
        filter_hook.chmod(0o755)
        W.git(self.ws, ["config", "filter.fixture.clean", "sh .git/hooks/filter-test"])
        W.git(
            self.ws, ["config", "filter.fixture.process", "sh .git/hooks/filter-test"]
        )
        W.git(self.ws, ["config", "filter.fixture.required", "true"])
        (path / ".gitattributes").write_text("*.py filter=fixture\n", encoding="utf-8")
        (path / "calc.py").write_text(
            "def add(a,b):\n    return a-b # modified\n", encoding="utf-8"
        )
        W.git_status(self.ws)
        self.assertFalse((path / "filter-executed").exists())
        self.run_tool(
            "prepare_code_branch",
            {
                "resource_id": self.r["id"],
                "branch": "agent/no-hooks",
                "expected_revision": W.current_revision(self.ws),
            },
        )
        self.assertFalse((path / "hook-executed").exists())
        self.assertEqual(W.git_status(self.ws)["branch"], "agent/no-hooks")
        self.grant(permission="read", write=True, execute=True, confirm=True)
        projects.snapshot(self.t["id"])
        self.run_tool(
            "run_project_check",
            {
                "resource_id": self.r["id"],
                "argv": [sys.executable, "-c", "import time;time.sleep(20)"],
                "timeout": 1,
                "expected_revision": W.current_revision(self.ws),
            },
        )
        op = self.call("GET", "/" + self.t["id"] + "/code-operations")[1]["operations"][
            -1
        ]
        self.assertNotIn(
            op["result"]["process"]["state"], {"running", "starting", "stopping"}
        )
        self.assertIsNotNone(op["result"]["process"]["exitCode"])
        sub = path / "nested"
        sub.mkdir()
        self.assertEqual(W.writer_lock_path(sub), W.writer_lock_path(path))


if __name__ == "__main__":
    unittest.main()
