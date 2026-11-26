"""Exercise history retirement at the provider and authorized read boundaries."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from loop import context_archive, context_manager, history_compaction, projects, workbench
from loop.context_inspection import request_owner
from loop.provider import Msg, OpenAICompatProvider, Reply
from tools.test_conversations import ConversationFixture


class HistoryGroupRetirementTests(ConversationFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        _, thread = self.call("POST", body={"workFolder": True})
        self.ident = thread["id"]
        self.directory = self.root / self.ident
        self.project = workbench.workspace(projects.conversation(self.ident)["workspaceId"])["path"]
        scope = projects.snapshot(self.ident)
        self.resource = scope["resources"][0]["id"]
        self.events = []
        self.session = SimpleNamespace(dir=self.directory, run_id="a" * 32,
            executor_core=object(), context_scope=scope, task_input={"id": "same-task"},
            emit=lambda act, **fields: self.events.append(dict(act=act, **fields)))
        self.provider = OpenAICompatProvider(base_url="http://127.0.0.1:1/v1",
            api_key="fixture", model="fixture")
        self.provider.max_tokens = 256
        self.provider.count_input_tokens = lambda body: history_compaction.measure(body)["tokens"]
        context_manager.bind_capacity(self.session, self.provider, 12000)

    def reads(self, label, count, size=7900):
        calls, replies = [], []
        for i in range(count):
            ident = f"{label}-{i}"
            path = ident + ".py"
            args = json.dumps(dict(resource_id=self.resource, path=path))
            calls.append(dict(id=ident, type="function", function=dict(name="inspect_code", arguments=args)))
            content = dict(ok=True, text=(ident + "\n") + "x" * size, revision=ident,
                path=path, startLine=1, endLine=1, hasMore=False,
                _harnessEvidence=dict(actionId=f"{count * 100 + i:032x}", runId=self.session.run_id))
            replies.append(Msg("tool", json.dumps(content), tool_call_id=ident))
        return [Msg("assistant", "Read the previous source pages.", tool_calls=calls), *replies]

    def source(self):
        failed = dict(id="failed-check", type="function", function=dict(name="run_project_check",
            arguments=json.dumps(dict(resource_id=self.resource, script="fixture"))))
        pending = dict(id="pending-check", type="function", function=dict(name="read_check_output",
            arguments=json.dumps(dict(resource_id=self.resource, process_id="pending"))))
        return [
            Msg("system", "Current instructions."), Msg("user", "Preserve the current failure and pending check."),
            *self.reads("oldest", 5), *self.reads("later", 1),
            Msg("assistant", "", tool_calls=[failed]),
            Msg("tool", '{"ok":false,"error":"CURRENT_FAILURE"}', tool_call_id=failed["id"]),
            Msg("assistant", "", tool_calls=[pending]),
            Msg("tool", '{"ok":true,"state":"running","operationId":"PENDING_CHECK"}', tool_call_id=pending["id"]),
            Msg("user", "Continue with the pending check and keep the failure."),
        ]

    def test_no_gain_retires_the_oldest_complete_batch_and_keeps_live_evidence(self):
        messages = self.source()
        untouched = [m.dump() for m in deepcopy(messages)]
        sent = []

        def stream(request, should_stop=None):
            sent.append(json.loads(request.data))
            yield b'data: {"choices":[{"delta":{"content":"continued"},"finish_reason":"stop"}]}'
            yield b'data: [DONE]'

        with request_owner(self.session, "executor"), \
                patch.object(self.provider, "chat", return_value=Reply("expanded " * 30000)) as summarize, \
                patch.object(self.provider, "_iter_lines", side_effect=stream):
            self.assertEqual(self.provider.chat_stream(messages, []).text, "continued")

        summarize.assert_called_once()
        self.assertEqual(len(sent), 1)
        model = sent[0]["messages"]
        context_manager.validate_pairs(model)
        visible = json.dumps(model)
        self.assertNotIn("oldest-", visible)
        self.assertIn("later-0", visible)
        self.assertIn("CURRENT_FAILURE", visible)
        self.assertIn("PENDING_CHECK", visible)
        self.assertIn(messages[1].content, visible)
        self.assertIn(messages[-1].content, visible)
        self.assertEqual(untouched, [m.dump() for m in messages])
        completed = next(row for row in self.events if row["act"] == "context_compaction"
                         and row["state"] == "completed")
        archive = context_archive.read_checkpoint(self.directory, projects.conversation(self.ident),
            completed["checkpointId"], 3, content_field="text", role="executor", root=self.project)
        self.assertTrue(archive["text"].startswith('"oldest-0\\n'))

    def test_resume_keeps_retired_rows_out_after_scope_revocation(self):
        from loop.tools import reset_call, set_call

        messages = self.source()
        with request_owner(self.session, "executor"), \
                patch.object(self.provider, "chat", return_value=Reply("expanded " * 30000)):
            first = context_manager.prepare(self.provider, self.provider._body(messages, []))
        ref = next(row["checkpointId"] for row in self.events if row["act"] == "context_compaction"
                   and row["state"] == "completed")
        token = set_call(dict(sessionId=self.ident, runId="b" * 32, role="executor"))
        self.addCleanup(reset_call, token)
        with patch("loop.code_service.RuntimeView._resolve_sid", side_effect=self.sv._resolve_sid):
            original = context_archive.tool_read(self.project, ref, 3)
            self.assertIn("oldest-0", original["text"])
            self.assertTrue(original["hasMore"])
            workbench.change_registry(lambda data: data["threads"][self.ident].update(resourceIds=[]))
            with self.assertRaises(workbench.Problem) as denied:
                context_archive.tool_read(self.project, ref, 3, original["nextOffset"], original["messageVersion"])
            self.assertEqual(denied.exception.code, "resource_denied")
        resumed = SimpleNamespace(dir=self.directory, run_id="b" * 32,
            executor_core=object(), context_scope=projects.snapshot(self.ident), task_input=self.session.task_input,
            emit=self.session.emit)
        context_manager.bind_capacity(resumed, self.provider, 12000)
        follow_up = self.provider._body([*messages, Msg("user", "Continue with the same pending check.")], [])
        with request_owner(resumed, "executor"), patch.object(self.provider, "chat") as summarize:
            actual = context_manager.prepare(self.provider, follow_up)
        summarize.assert_not_called()
        context_manager.validate_pairs(actual["messages"])
        self.assertNotIn("oldest-", json.dumps(actual))
        self.assertNotIn(ref, json.dumps(actual))
        self.assertIn("当前不可回读", json.dumps(actual, ensure_ascii=False))
        self.assertEqual(first["messages"][-1]["content"], messages[-1].content)

    def test_checkpoint_write_failure_leaves_the_original_batch_and_sends_no_candidate(self):
        messages = self.source()
        with request_owner(self.session, "executor"), \
                patch.object(self.provider, "chat", return_value=Reply("expanded " * 30000)), \
                patch.object(self.provider, "_iter_lines") as stream, \
                patch("loop.context_manager.atomic_json", side_effect=OSError("fixture disk failure")):
            with self.assertRaisesRegex(context_manager.ContextBlocked, "检查点未能持久保存"):
                self.provider.chat_stream(messages, [])
        stream.assert_not_called()
        self.assertFalse((self.directory / "context-work-summary.json").exists())
        self.assertFalse(any(row.get("state") == "completed" for row in self.events))
        self.assertEqual(messages[2].tool_calls[0]["id"], "oldest-0")

    def test_successful_summary_precedes_retirement(self):
        messages = [Msg("system", "Current instructions."), Msg("user", "Read the project.")]
        for i in range(9):
            messages.extend(self.reads("past-" + str(i), 1))
        messages.append(Msg("user", "Continue."))
        with request_owner(self.session, "executor"), \
                patch.object(self.provider, "chat", return_value=Reply("Small historical checkpoint.")):
            result = context_manager.prepare(self.provider, self.provider._body(messages, []))
        completed = next(row for row in self.events if row["act"] == "context_compaction"
                         and row["state"] == "completed")
        self.assertEqual(completed["strategy"], "effective-work-checkpoint")
        self.assertNotIn("[model-context:retired-history]", json.dumps(result))

    def test_mixed_batch_and_explicitly_required_read_stay_intact(self):
        messages = self.source()
        mixed = self.reads("mixed", 2, 150)
        mixed[-1].content = '{"ok":false,"error":"MIXED_BATCH_FAILURE"}'
        messages[2:2] = [*mixed, *self.reads("required", 1, 150)]
        messages[-1].content += " Keep the original read of required-0.py."
        with request_owner(self.session, "executor"), \
                patch.object(self.provider, "chat", return_value=Reply("expanded " * 30000)):
            prepared = context_manager.prepare(self.provider, self.provider._body(messages, []))
        context_manager.validate_pairs(prepared["messages"])
        ids = {row.get("tool_call_id") for row in prepared["messages"] if row["role"] == "tool"}
        self.assertTrue({"mixed-0", "mixed-1", "required-0"}.issubset(ids))
        self.assertFalse(any(ident.startswith("oldest-") for ident in ids))
        self.assertIn("MIXED_BATCH_FAILURE", json.dumps(prepared))


if __name__ == "__main__":
    unittest.main()
