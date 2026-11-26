"""Behavioral regressions: fail-closed gates, exact approvals and native isolation."""

import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from loop import permissions as P, policy, baseline, workbench as W
from loop.tools import set_call, reset_call


class PermissionSafety(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="owned-permission-tests-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.patch = patch.object(W.P, "data_root", return_value=self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.owner = dict(runId="a"*32,sessionId="b"*32,actionId="c"*32,tool="owned")
        self.token = set_call(self.owner)
        self.addCleanup(reset_call,self.token)

    def mode(self, value):
        old = P.profile()
        return P.route("POST",[],dict(mode=value,expectedRevision=old["revision"],confirm=True))[1]

    def test_baseline_logging_failure_still_denies(self):
        with patch.object(baseline,"hit",return_value={"synthetic":True}),patch.object(baseline,"record",side_effect=OSError("owned failure")):
            self.assertFalse(policy.Policy("trusted",root=self.root,rules={}).check("read_file",{"path":str(self.root/"owned")},kind="read")[0])
            from loop.tools import _protected
            self.assertFalse(_protected(self.root/"owned","read")["ok"])

    def test_unisolated_hook_cannot_bypass_controlled_mode(self):
        from loop import hooks
        with patch.object(hooks,"load",return_value={"PreToolUse":[dict(command="echo OWNED_ONLY")]}),patch("subprocess.Popen") as host:
            with self.assertRaises(W.Problem):hooks.run("PreToolUse",{},self.root)
            host.assert_not_called()

    def test_broken_policy_does_not_remove_restrictions(self):
        cfg = self.root/policy.RULES_FILE
        cfg.parent.mkdir(parents=True)
        cfg.write_text("{broken")
        with patch.object(policy,"HOME",self.root/"fake-home"):
            gate = policy.Policy("trusted",root=self.root)
            self.assertTrue(gate.rules["errors"])
            self.assertFalse(gate.check("run_check",{"cmd":["python","-c","print(1)"]},kind="exec")[0])

    def test_default_does_not_migrate_legacy_trusted(self):
        self.assertEqual(P.profile()["mode"],"ask")

    def test_old_full_approval_never_enables_the_host_terminal(self):
        (P.directory() / "profile.json").write_text(json.dumps({"mode":"full", "revision":5}))
        self.assertEqual(P.effective_sandbox(), "workspace-write")
        for value in ("danger-full-access", ["not-a-mode"]):
            with self.assertRaises(W.Problem):
                P.route("POST", [], dict(sandboxMode=value, expectedRevision=5))
        self.assertEqual(P.profile()["revision"], 5)

    def test_terminal_downgrade_survives_a_later_explicit_raise_for_the_same_run(self):
        self.mode("full")
        self.assertEqual(P.effective_sandbox(), "danger-full-access")
        self.mode("ask")
        self.mode("full")
        self.assertEqual(P.effective_sandbox(), "workspace-write")

    def test_legacy_mixed_modes_never_upgrade_and_new_defaults_are_independent(self):
        name = self.owner["sessionId"]
        path = P.directory() / "profile.json"
        path.write_text(json.dumps(dict(mode="auto", sandboxMode="danger-full-access", revision=8)))
        with patch.object(W, "registry", return_value={"threads": {name: {"projectless":True}}}):
            self.assertEqual(P.profile(name)["sandboxMode"], "workspace-write")
            P.change_preset(dict(mode="full", expectedRevision=8, confirm=True))
            self.assertEqual(P.profile(name)["mode"], "auto")
            self.assertEqual(P.new_chat_profile({})["mode"], "full")
            path.write_text(json.dumps(dict(mode="ask", sandboxMode="read-only", revision=8)))
            self.assertEqual(P.profile(name)["sandboxMode"], "read-only")
            P.change_preset(dict(mode="ask", expectedRevision=8), name)
            self.assertEqual(P.profile(name)["sandboxMode"], "workspace-write")
            self.assertEqual(P.default_profile()["mode"], "full")
        with self.assertRaises(W.Problem):
            P.new_chat_profile({"permissionMode":"full"})

    def test_frozen_mixed_legacy_run_stays_in_workspace_after_explicit_raise(self):
        old = dict(mode="auto", sandboxMode="danger-full-access", revision=7)
        raised = dict(mode="full", sandboxMode="danger-full-access", revision=8)
        self.assertEqual(P._sandbox_ceiling(old, raised), "workspace-write")

    def test_full_requires_confirmation_and_revision(self):
        with self.assertRaises(W.Problem): P.route("POST",[],dict(mode="full",expectedRevision=1))
        self.mode("auto")
        with self.assertRaises(W.Problem): P.route("POST",[],dict(mode="full",expectedRevision=1,confirm=True))

    def test_raise_applies_next_run_lower_applies_current_run(self):
        P.freeze(self.owner["runId"],self.owner["sessionId"])
        self.mode("full")
        self.assertEqual(P.effective_mode(),"ask")
        token=set_call(dict(self.owner,runId="d"*32))
        try:
            self.assertEqual(P.effective_mode(),"full")
            self.mode("ask")
            self.assertEqual(P.effective_mode(),"ask")
        finally: reset_call(token)

    def test_broken_permission_profile_denies(self):
        (P.directory()/"profile.json").write_text("[]")
        with self.assertRaises(W.Problem): P.effective_mode()

    def test_auto_review_uses_exact_request_once(self):
        self.mode("auto")
        from loop import plugins
        path=str(Path(__file__).resolve().parents[1]/'examples'/'plugin-catalog'/'studio-v1')
        installed=plugins.install(path,plugins.preview(path)['fingerprint'])
        enabled=plugins.change(installed['id'],dict(expectedRevision=installed['revision'],remoteEnabled=True,confirm=True))
        action=dict(kind="remote-plugin",pluginId=enabled['id'],pluginRevision=enabled['revision'],
                    destination="隔离本机服务:notes",tool="read_note",arguments={"query":"owned"})
        with patch.object(P,"_review",return_value=dict(decision="allow",reason="owned fixture")) as reviewer:
            P.authorize(action)
            P.authorize(dict(action,arguments={"query":"different"}))
            self.assertEqual(reviewer.call_count,2)
        rows=P.listing()
        self.assertEqual({r["state"] for r in rows},{"consumed"})
        self.assertNotEqual(rows[0]["fingerprint"],rows[1]["fingerprint"])
        with self.assertRaises(W.Problem): P.route("POST",[rows[0]["id"]],dict(decision="allow",fingerprint=rows[0]["fingerprint"]))

    def test_denial_stops_other_tool_workarounds(self):
        self.mode("auto")
        with patch.object(P,"_review",return_value=dict(decision="deny",reason="owned denial")):
            with self.assertRaises(W.Problem): P.authorize(dict(kind="owned",tool="first"))
        with self.assertRaises(W.Problem): P.effective_mode()
        self.assertEqual(P.listing()[0]["state"],"denied")

    def test_unavailable_reviewer_never_auto_allows(self):
        self.mode("auto")
        def decide():
            until=time.monotonic()+5
            while time.monotonic()<until:
                rows=P.listing()
                if rows and rows[0]["state"]=="pending":
                    row=rows[0]
                    P.route("POST",[row["id"]],dict(decision="deny",fingerprint=row["fingerprint"]))
                    return
                time.sleep(.02)
        t=threading.Thread(target=decide);t.start()
        with patch.object(P,"_review",side_effect=RuntimeError("owned offline")):
            with self.assertRaises(W.Problem): P.authorize(dict(kind="owned"))
        t.join(5)
        self.assertFalse(t.is_alive())
        self.assertEqual(P.listing()[0]["reviewer"],"human")

    def test_no_model_or_memory_can_grant_via_bad_action(self):
        self.mode("full")
        with self.assertRaises(W.Problem): P.authorize(dict(api_key="sk-"+"a"*48))

    def test_unsupported_transport_has_no_host_fallback(self):
        from loop import safe_executor as X
        with patch("subprocess.Popen") as host:
            with self.assertRaises(W.Problem): X.popen([sys.executable],stdin=-1,stdout=-1,stderr=-1,cwd=self.root)
            host.assert_not_called()

    def test_downgrade_cannot_be_undone_inside_old_run(self):
        self.mode("full")
        P.freeze(self.owner["runId"],self.owner["sessionId"])
        self.mode("ask");self.mode("full")
        self.assertEqual(P.effective_mode(),"ask")

    def test_child_inherits_parent_ceiling(self):
        P.freeze(self.owner["runId"],self.owner["sessionId"])
        self.mode("full")
        child={"threads":{"child":dict(subtaskOf=dict(runId=self.owner["runId"],parentId=self.owner["sessionId"]))}}
        with patch.object(W,"registry",return_value=child):
            self.assertEqual(P.freeze("d"*32,"child")["mode"],"ask")
        with patch.object(W,"registry",return_value={"threads":{"child":dict(subtaskOf=dict(runId="missing",parentId=self.owner["sessionId"]))}}):
            with self.assertRaises(W.Problem):P.freeze("e"*32,"child")

    def test_expired_approval_stops_without_execution(self):
        with patch.object(P,"TTL",0):
            with self.assertRaises(W.Problem) as caught:P.authorize(dict(kind="owned"))
        self.assertEqual(caught.exception.code,"approval_expired")
        self.assertEqual(P.listing()[0]["state"],"expired")

    def test_permission_change_revokes_automatic_approval(self):
        self.mode("auto")
        def review(*args):
            self.mode("ask")
            return dict(decision="allow",reason="owned fixture")
        with patch.object(P,"_review",side_effect=review):
            with self.assertRaises(W.Problem):P.authorize(dict(kind="owned"))
        self.assertEqual(P.listing()[0]["state"],"revoked")

    def test_human_approval_binds_fingerprint_and_consumes_once(self):
        session = self.owner["sessionId"]
        P.save_new_chat_profile(session, dict(mode="ask", sandboxMode="workspace-write", revision=7))
        self.assertNotEqual(P.default_profile()["revision"], P.profile(session)["revision"])
        errors=[]
        def work():
            token=set_call(self.owner)
            try:P.authorize(dict(kind="owned",arguments={"value":"exact"}))
            except Exception as e:errors.append(e)
            finally:reset_call(token)
        thread=threading.Thread(target=work);thread.start()
        until=time.monotonic()+5
        while not P.listing() and time.monotonic()<until:time.sleep(.02)
        row=P.listing()[0]
        with self.assertRaises(W.Problem):P.route("POST",[row["id"]],dict(decision="allow",fingerprint="wrong"))
        self.assertNotIn("arguments",P.route("GET",[],{})[1]["requests"][0]["action"])
        self.assertEqual(P.route("GET",[row["id"]],{})[1]["action"]["arguments"],{"value":"exact"})
        # The user's UI submits without an L0 tool call in its HTTP thread.
        ui = set_call({})
        try:
            P.route("POST",[row["id"]],dict(decision="allow",fingerprint=row["fingerprint"]))
        finally:
            reset_call(ui)
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors,[])
        self.assertEqual(P.listing()[0]["state"],"consumed")

    def test_remote_plugin_reviews_before_contact_and_reuses_exact_manual_decision(self):
        from loop import plugin_runtime as runtime
        session = self.owner["sessionId"]
        package = dict(id="sample", revision=1, fingerprint="sample", writeEnabled=True)
        target = dict(name="sample", url="https://mcp.example.invalid/tools", transport="http")
        lookup = dict(plugin_id="sample", server="sample")
        events = []
        client = Mock()
        client.request.side_effect = lambda *a: events.append("invoke") or {"content":[]}
        def connect(*args):
            events.append("connect")
            return client
        def permit(action, *, human_only=False, **kwargs):
            events.append("human" if human_only else "review")
            return "one-decision"
        with patch.object(runtime, "selected", return_value=(package, target)), patch.object(runtime, "connect", side_effect=connect), patch.object(P, "authorize", side_effect=permit) as approval:
            for readonly, human, expected in [
                (True, False, ["review","connect","invoke"]),
                (False, True, ["review","connect","invoke"]),
                (False, False, ["review","connect","human","connect","invoke"]),
            ]:
                spec = dict(name="operation", inputSchema={"type":"object"},
                            annotations={"readOnlyHint":readonly,"destructiveHint":False})
                client.tools.return_value = [spec]
                call = dict(lookup, tool="operation", schema_hash=runtime.schema_hash(package,"sample",spec), arguments={"path":"sample"})
                with self.subTest(readonly=readonly, human=human), patch.object(P,"approved_by_user",return_value=human):
                    events.clear()
                    self.assertTrue(runtime.invoke(session,"call",call)["ok"])
                    self.assertEqual(events, expected)
            events.clear()
            approval.side_effect = W.Problem("approval_denied", "denied", 403)
            with patch("loop.plugin_connections.github_account_authorization", return_value="fixture"), patch("loop.plugin_connections.github_installation_page") as lookup_service:
                for operation, args in [("inspect",lookup),("github-repositories",lookup),("call",call)]:
                    with self.subTest(denied=operation), self.assertRaises(W.Problem):
                        runtime.invoke(session,operation,args)
                lookup_service.assert_not_called()
            self.assertEqual(events, [])

    def test_reviewer_has_no_tools_and_consumes_shared_budget(self):
        from loop.provider import Reply
        from loop.execution_policy import BudgetExhausted
        run=P.freeze(self.owner["runId"],self.owner["sessionId"])
        P.bind_intent(run['runId'],run['sessionId'],'Owned task')
        path=self.root/"runs"/run["sessionId"]/"execution-runs"/(run["runId"]+".json")
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(dict(config=dict(modelCalls=1),usedCalls=0,byRole={})),encoding="utf8")
        class Reviewer:
            def chat(this,messages,tools):
                self.assertEqual(tools,[])
                self.assertEqual([m.role for m in messages],["system","user"])
                self.assertTrue(all(not m.reasoning for m in messages))
                return Reply(text=json.dumps(dict(decision="allow",reason="owned")))
        with patch("loop.provider.OpenAICompatProvider",return_value=Reviewer()),patch("loop.models.resolve",return_value={}):
            self.assertEqual(P._review(dict(kind="owned"),run)["decision"],"allow")
            with self.assertRaises(BudgetExhausted):P._review(dict(kind="owned"),run)
        self.assertEqual(json.loads(path.read_text(encoding="utf8"))["byRole"],{"safety":1})

    def test_actual_loop_auto_approval_records_action_reference(self):
        from loop import loop as L
        from loop.provider import MockProvider
        from loop.session import Session
        from loop.tools import Registry,Tool
        self.mode("auto")
        touched=[]
        registry=Registry().add(Tool("owned_external","owned fixture",{"type":"object","properties":{}},
            lambda:dict(ok=True,count=len(touched.append("once") or touched)),zone="offflow",danger=True,kind="read"))
        session=Session(self.root/"runs"/"owned-loop")
        with patch.object(P,"_review",return_value=dict(decision="allow",reason="owned fixture")):
            result=L.run("owned task",MockProvider([{"tool":"owned_external","args":{}},{"text":"done"}]),registry,
                lambda _:dict(ok=True),session=session,max_turns=2,policy=policy.Policy("workspace",root=self.root))
        self.assertEqual(result["status"],"success")
        self.assertEqual(touched,["once"])
        rows=[json.loads(line) for line in session.events_path.read_text(encoding="utf8").splitlines()]
        self.assertTrue(any(r.get("approvalRefs")==[P.listing()[0]["id"]] for r in rows))

    def test_actual_loop_denial_never_executes_or_retries(self):
        from loop import loop as L
        from loop.provider import MockProvider
        from loop.session import Session
        from loop.tools import Registry,Tool
        self.mode("auto")
        touched=[]
        registry=Registry().add(Tool("owned_external","owned fixture",{"type":"object","properties":{}},
            lambda:touched.append("bad"),zone="offflow",danger=True,kind="read"))
        with patch.object(P,"_review",return_value=dict(decision="deny",reason="owned fixture")):
            result=L.run("owned task",MockProvider([{"tool":"owned_external","args":{}},{"tool":"owned_external","args":{}},{"text":"done"}]),registry,
                lambda _:dict(ok=True),session=Session(self.root/"runs"/"owned-denial"),max_turns=3,policy=policy.Policy("workspace",root=self.root))
        self.assertEqual(result["status"],"needs_human")
        self.assertEqual(touched,[])

    @unittest.skipUnless(os.name=="nt","Windows job required")
    def test_full_access_downgrade_terminates_owned_family(self):
        from loop.safe_executor import popen
        from loop import runtime as RT
        import subprocess
        self.mode("full")
        code="import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']); print(p.pid,flush=True);time.sleep(30)"
        p=popen([sys.executable,"-c",code],cwd=self.root,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        try:
            child=int(p.stdout.readline())
            self.mode("ask")
            self.assertNotEqual(p.wait(timeout=8),0)
            self.assertIsNone(RT.identity(child))
        finally:
            if p.poll() is None:p.kill()
            p.stdout.close();p.stderr.close()


@unittest.skipUnless(os.name=="nt","Windows AppContainer required")
class NativeIsolation(unittest.TestCase):
    def test_python_temporary_directories_and_native_command_decoding(self):
        from loop.safe_executor import start

        script = """
import locale
import os
from pathlib import Path
import subprocess
import tempfile

with tempfile.TemporaryDirectory() as first:
    assert Path(first).parent == Path(os.environ["TMPDIR"])
    with tempfile.TemporaryDirectory(dir=first) as second:
        (Path(second) / "proof").write_text("ok", encoding="utf8")
    assert not Path(second).exists()
assert not Path(first).exists()

child = subprocess.run(
    ["harness_command_that_does_not_exist"], shell=True, capture_output=True, text=True
)
assert child.returncode != 0 and child.stderr
assert subprocess._text_encoding() == locale.getencoding()
print("TEMP_AND_OUTPUT_OK \u6d4b\u8bd5")
"""
        with tempfile.TemporaryDirectory(prefix="owned-native-temp-") as directory:
            p = start([sys.executable, "-u", "-c", script], cwd=directory, time_limit=8)
            out, err = p.communicate(timeout=18)
        self.assertEqual(p.returncode, 0, (out.decode("utf8", "replace"), err.decode("utf8", "replace")))
        self.assertIn("TEMP_AND_OUTPUT_OK 测试", out.decode("utf8"))
        self.assertNotIn(b"UnicodeDecodeError", err)

    def test_legacy_contract_remains_isolated_even_in_full_mode(self):
        import importlib.util
        spec=importlib.util.spec_from_file_location("owned_contract",Path(__file__).resolve().parents[1]/"bin/contract.py")
        contract=importlib.util.module_from_spec(spec);spec.loader.exec_module(contract)
        with tempfile.TemporaryDirectory(prefix="owned-contract-security-") as td:
            root=Path(td);work=root/"work";work.mkdir();outside=root/"outside.txt";outside.write_text("owned canary")
            script="from pathlib import Path\ntry:Path("+repr(str(outside))+").read_text();raise AssertionError('outside readable')\nexcept PermissionError:pass\nPath('generated.txt').write_text('staged')\nprint('OWNED_CONTRACT_OK')\n"
            (work/"check.py").write_text(script,encoding="utf8")
            with patch.object(W.P,"data_root",return_value=root):
                P.route("POST",[],dict(mode="full",expectedRevision=1,confirm=True))
                result=contract.run_cmd("python check.py",cwd=work,timeout=30)
            self.assertEqual(result["rc"],0,result)
            self.assertIn("OWNED_CONTRACT_OK",result["out"])
            self.assertFalse((work/"generated.txt").exists())

    def test_real_python_boundary_and_process_cleanup(self):
        from loop.safe_executor import start
        from loop import runtime as RT
        with tempfile.TemporaryDirectory(prefix="owned-native-security-") as td:
            root=Path(td);work=root/"workspace";work.mkdir()
            canary=root/"outside.txt";canary.write_text("owned outside canary")
            (work/"inside.txt").write_text("owned inside")
            (work/".env").write_text("OWNED_SECRET=fixture")
            listener=socket.socket();listener.bind(("127.0.0.1",0));listener.listen()
            self.addCleanup(listener.close)
            script='''import json, pathlib, os, socket, subprocess, sys
r={"inside":pathlib.Path("inside.txt").read_text(),"env":os.getenv("SYNTHETIC_API_KEY"),"dot_env":pathlib.Path(".env").exists()}
try: pathlib.Path(CANARY).read_text();r["outside_blocked"]=False
except OSError:r["outside_blocked"]=True
s=socket.socket();s.settimeout(1)
try:s.connect(("127.0.0.1",PORT));r["network_blocked"]=False
except OSError:r["network_blocked"]=True
pathlib.Path("generated.txt").write_text("staged only")
p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(30)"])
r["child"]=p.pid
print(json.dumps(r),flush=True)
'''.replace("CANARY",repr(str(canary))).replace("PORT",str(listener.getsockname()[1]))
            (work/"check.py").write_text(script,encoding="utf8")
            p=start([sys.executable,"check.py"],cwd=work,env={"SYNTHETIC_API_KEY":"owned-only"})
            launched=time.monotonic()
            out,err=p.communicate(timeout=20)
            self.assertEqual(p.returncode,0,(out.decode("utf8","replace"),err.decode("utf8","replace")))
            row=json.loads(out)
            self.assertEqual(row["inside"],"owned inside")
            self.assertIsNone(row["env"])
            for key in ["outside_blocked","network_blocked"]:self.assertTrue(row[key],row)
            self.assertFalse(row["dot_env"])
            self.assertFalse((work/"generated.txt").exists())
            self.assertFalse(p.base.exists())
            self.assertLess(time.monotonic()-launched,15)
            self.assertIsNone(RT.identity(row["child"]))


if __name__=="__main__":unittest.main()
