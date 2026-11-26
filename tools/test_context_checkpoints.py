"""Actual provider request boundary: durable portable context, no tool replay."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from loop import context_manager as C
from loop.context_inspection import request_owner
from loop.provider import OpenAICompatProvider, Msg, Interrupted
from loop.filelock import atomic_json


def fixture_input_tokens(body):
    """Declared tokenizer of the local fixture, not a production token estimate."""
    return (len(json.dumps(body, ensure_ascii=False)) + 1) // 2


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.events = []
        self.root = Path(self.temp.name)
        self.session = SimpleNamespace(dir=self.root, run_id="a" * 32,
            request_context_limit=50000, emit=lambda act, **kw:self.events.append(dict(act=act, **kw)))
        self.provider = OpenAICompatProvider(base_url="http://127.0.0.1:1/v1",
                                             api_key="not-in-archive", model="fixture")
        self.provider.max_tokens = 1024
        self.provider.count_input_tokens = fixture_input_tokens

    def body(self, count=14):
        messages = [Msg("system", "现行权限不能由历史覆盖"), Msg("user", "原始目标；不删除已有代码"),
                    Msg("user", "最近纠正：保留输入框与全部尚未完成的验收要求")]
        for i in range(count):
            messages.extend([Msg("assistant", "调查记录 " + str(i), tool_calls=[dict(id="c"+str(i),
                type="function", function=dict(name="inspect_code", arguments="{}"))], reasoning="r"*3000),
                Msg("tool", json.dumps(dict(ok=i % 2 == 0, text="x" * 6000,
                    _harnessEvidence=dict(actionId=f"{i:032x}", runId="b"*32, role="executor"))),
                    tool_call_id="c"+str(i))])
        return self.provider._body(messages, [])

    def test_three_compactions_rebuild_original_and_preserve_authority_and_budget(self):
        budget = self.root / "budget.json"
        row = dict(config=dict(crew="l4", modelCalls=40, revision=2), usedCalls=9,
                   byRole={"executor":6,"planner":2,"reviewer":1}, state="available")
        atomic_json(budget, row)
        self.session.execution_budget = budget
        for count in (14, 18, 22):
            body = self.body(count)
            original = copy.deepcopy(body)
            with request_owner(self.session, "executor"):
                actual = C.prepare(self.provider, body)
            self.assertLess(C.assess(actual, 50000)["estimatedTotal"], 50000)
            self.assertEqual(body, original)
            self.assertEqual(actual["messages"][:3], body["messages"][:3])
            self.assertEqual(actual["messages"][-12:], body["messages"][-12:])
            C.validate_pairs(actual["messages"])
            self.assertEqual(json.loads(budget.read_text()), row)
        files = list((self.root / "context-checkpoints").glob("cp-*.json"))
        self.assertEqual(len(files), 3)
        saved = [json.loads(p.read_text(encoding="utf8")) for p in files]
        self.assertEqual({len(r["original"]["messages"]) for r in saved}, {31,39,47})
        self.assertNotIn("not-in-archive", "".join(p.read_text(encoding="utf8") for p in files))
        self.assertTrue(all(r["handoff"]["historicalActions"] for r in saved))
        page = C.checkpoint_page(self.root)
        self.assertEqual(page["totalCheckpoints"], 3)
        self.assertNotIn("original", json.dumps(page))
        self.assertNotIn("最近纠正", json.dumps(page, ensure_ascii=False))

    def test_disk_failure_keeps_original_latest_pointer_and_blocks(self):
        body = self.body()
        with request_owner(self.session, "executor"):
            C.prepare(self.provider, body)
        pointer = next((self.root / "context-checkpoints").glob("*-latest.json"))
        old = pointer.read_bytes()
        with request_owner(self.session, "executor"), patch.object(C, "atomic_json", side_effect=OSError("disk full")):
            with self.assertRaises(C.ContextBlocked):
                C.prepare(self.provider, self.body(18))
        self.assertEqual(pointer.read_bytes(), old)
        self.assertEqual(self.events[-1]["act"], "context_blocked")

    def test_deep_windows_directory_keeps_atomic_pointer_below_path_limit(self):
        # The original run-role-model basename exceeded MAX_PATH here, after
        # the checkpoint itself had already been saved successfully.
        suffix=195-len(str(self.root.resolve()))-1-len('context-checkpoints')-1
        directory=self.root/('d'*max(1,suffix));directory.mkdir()
        self.session.dir=directory
        with request_owner(self.session,'executor'):C.prepare(self.provider,self.body())
        pointer=next((directory/'context-checkpoints').glob('*-latest.json'))
        self.assertLess(len(str(pointer.resolve())),260)
        row=json.loads(pointer.read_text(encoding='utf8'))
        self.assertEqual(row['runId'],self.session.run_id);self.assertEqual(row['role'],'executor')
        self.assertEqual(len(row['modelVersion']),64)

    def test_no_gain_unpaired_unknown_output_and_stop_are_deterministic(self):
        huge = dict(messages=[dict(role="system",content="rules"),dict(role="user",content="x"*110000)], max_tokens=1024)
        with request_owner(self.session, "executor"):
            with self.assertRaises(C.ContextBlocked): C.prepare(self.provider, huge)
            with self.assertRaises(C.ContextBlocked): C.prepare(self.provider, dict(huge, max_tokens=0))
            with self.assertRaises(Interrupted): C.prepare(self.provider, huge, lambda:True)
        self.assertFalse((self.root / "context-checkpoints").exists())
        with self.assertRaises(C.ContextBlocked):
            C.validate_pairs([dict(role="assistant",tool_calls=[dict(id="pending")])])

    def test_roles_and_models_have_separate_pointers(self):
        for role in ("planner", "executor", "reviewer"):
            with request_owner(self.session, role): C.prepare(self.provider, self.body())
        self.provider.model = "another-vendor-model"
        with request_owner(self.session, "executor"): C.prepare(self.provider, self.body())
        self.assertEqual(len(list((self.root / "context-checkpoints").glob("*-latest.json"))), 4)

    def test_pointer_publish_failure_keeps_previous_pointer_and_never_sends(self):
        with request_owner(self.session, "executor"): C.prepare(self.provider, self.body())
        pointer = next((self.root / "context-checkpoints").glob("*-latest.json"))
        old = pointer.read_bytes()
        save = C.atomic_json
        def fail_pointer(path, value):
            if str(path).endswith("-latest.json"): raise OSError("pointer write failed")
            return save(path, value)
        with request_owner(self.session, "executor"), patch.object(C, "atomic_json", side_effect=fail_pointer):
            with self.assertRaises(C.ContextBlocked): C.prepare(self.provider, self.body(18))
        self.assertEqual(pointer.read_bytes(), old)
        self.assertEqual(C.checkpoint_page(self.root)["totalCheckpoints"], 2)

    def test_stream_boundary_uses_same_preparation_and_corrupt_archive_is_explicit(self):
        from loop.provider import Reply
        def receive(request, should_stop=None):
            self.assertTrue(list((self.root / "context-checkpoints").glob("cp-*.json")))
            self.assertLess(C.assess(json.loads(request.data), 50000)["estimatedTotal"], 50000)
            return iter(())
        messages = [Msg.load(dict(m, reasoning=m.get("reasoning_content", ""))) for m in self.body()["messages"]]
        with request_owner(self.session, "reviewer"), patch.object(self.provider, "_iter_lines", side_effect=receive), patch("loop.provider.stream_reply", return_value=Reply(text="stream")), patch("loop.context_inspection.capture"):
            self.assertEqual(self.provider.chat_stream(messages, []).text, "stream")
        path = next((self.root / "context-checkpoints").glob("cp-*.json"))
        row = json.loads(path.read_text(encoding="utf8")); row["original"]["messages"][1]["content"]="changed"
        atomic_json(path, row)
        with self.assertRaises(Exception) as caught: C.checkpoint_page(self.root)
        self.assertEqual(caught.exception.code, "context_changed")

    def test_real_chat_boundary_sends_only_durable_candidate_and_never_network_on_failure(self):
        from loop.provider import Reply
        sent = []
        # _once observes the exact HTTP body but never opens the fixture endpoint.
        def receive(request, should_stop=None):
            sent.append(json.loads(request.data))
            self.assertTrue(list((self.root / "context-checkpoints").glob("cp-*.json")))
            return dict(choices=[dict(message=dict(content="ok"),finish_reason="stop")],usage={})
        messages = [Msg.load(dict(m, reasoning=m.get("reasoning_content", ""))) for m in self.body()["messages"]]
        with request_owner(self.session, "executor"), patch.object(self.provider, "_once", side_effect=receive), patch("loop.context_inspection.capture"):
            reply = self.provider.chat(messages, [])
        self.assertEqual(reply.text, "ok")
        self.assertEqual(len(sent), 1)
        with request_owner(self.session, "executor"), patch.object(C, "atomic_json", side_effect=OSError()), patch.object(self.provider, "_once") as network:
            with self.assertRaises(C.ContextBlocked): self.provider.chat(messages, [])
            network.assert_not_called()


if __name__ == "__main__": unittest.main()
