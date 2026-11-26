import json
import tempfile
import unittest
from pathlib import Path

from loop import task_input
from loop.session import Session


class TaskHistoryBoundaryTests(unittest.TestCase):
    def test_followup_after_expired_approval_keeps_goal_and_completed_tool_turn(self):
        question = "你帮我找找bug"
        note = "继续，请在本轮已有的项目授权范围内核查，按实际证据回答。"
        expired = json.dumps({"ok": False, "code": "approval_expired"})
        messages = [dict(role="user", content="earlier task"), dict(role="user", content=question),
                    dict(role="assistant", tool_calls=[dict(id="call-1")]),
                    dict(role="tool", tool_call_id="call-1", content=expired)]
        original = dict(id="bug-hunt", revision=1, messageStart=1)
        checkpoint = dict(run_id="expired-run", status="needs_human", waitQuestion=False,
                          taskInput=original, msgs=messages)
        for text in (note, "继续, 请查一下", "恢复上一轮，按现有授权继续", "Continue, stay within the project"):
            with self.subTest(text=text):
                kind = task_input.classify(text, checkpoint)
                self.assertEqual(kind, "amend")
                binding = task_input.bind(original, kind, "next-run", checkpoint)
                self.assertEqual((binding["id"], binding["revision"]), ("bug-hunt", 2))
                old = dict(taskId="bug-hunt", entries=[dict(kind="goal", text=question)])
                material = task_input.effective(old, binding, question, text, checkpoint)
                self.assertEqual(material["entries"], [*old["entries"], dict(kind="amend", text=text)])
                self.assertEqual(messages[task_input.message_start(checkpoint, kind):], messages[1:])

    def test_explicit_topic_switch_starts_fresh_and_pending_answers_keep_priority(self):
        checkpoint = dict(run_id="old-run", status="needs_human", waitQuestion=False,
                          msgs=[dict(role="user", content="previous goal")])
        for text in ("新任务：看构建配置", "换个任务，整理文档", "继续，换个任务：解释配置",
                     "Continue, new task: review the build", "继续阅读另一个项目"):
            with self.subTest(text=text):
                kind = task_input.classify(text, checkpoint)
                self.assertEqual(kind, "new")
                self.assertEqual(task_input.message_start(checkpoint, kind), 1)
                self.assertEqual(task_input.bind({}, kind, "next-run", checkpoint)["id"], "next-run")
        self.assertEqual(task_input.classify("继续，不要换题", checkpoint), "amend")
        self.assertEqual(task_input.classify("继续，检查新任务：按钮的文字", checkpoint), "amend")
        self.assertEqual(task_input.classify("继续，选第一项", dict(checkpoint, waitQuestion=True)), "answer")
        self.assertEqual(task_input.classify("继续，按原要求", {}), "new")

    def test_previous_public_exchange_contains_the_end_of_the_reply_and_user_question(self):
        options = "2D 绘图编辑器、DWG/DXF 查看转换、三维模型预览"
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            event = {"act": "user", "seq": 1, "run_id": "cad", "text": "CAD 插件怎么开工？"}
            (directory / "events.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")
            checkpoint = dict(run_id="cad", status="success", public_reply_persisted=True,
                              final="首版路线。" + ("构建环境需要确认。" * 220) + options,
                              taskInput={"userMessageSeq": 1, "readBindings": {"code": "version1"}},
                              effectiveRequirements={"entries": [{"text": "模型请求预置正文"}]})
            text, bindings = task_input.previous_public_exchange(checkpoint, directory)
        pair = json.loads(text)
        self.assertEqual(pair["上一条用户原话"], "CAD 插件怎么开工？")
        self.assertTrue(pair["上一轮公开答复"].endswith(options))
        self.assertIn("…", pair["上一轮公开答复"])
        self.assertLess(len(text), 2200)
        self.assertEqual(bindings, {"code": "version1"})
        self.assertIsNone(task_input.previous_public_exchange(dict(checkpoint, status="paused"), Path(".")))
        self.assertIsNone(task_input.previous_public_exchange(dict(checkpoint, public_reply_persisted=False), Path(".")))

    def test_new_task_and_resume_preserve_the_original_prefix_exactly_once(self):
        with tempfile.TemporaryDirectory() as folder:
            session = Session(Path(folder))
            original = [dict(role="user", content="old task"),
                        dict(role="assistant", content="old result")]
            session.save(msgs=original, taskInput=dict(id="first", kind="new"))
            start = task_input.message_start(session.load(), "new")
            self.assertEqual(start, 2)
            session.task_history_start = start
            new = [dict(role="user", content="new task")]
            session.save(msgs=new, taskInput=dict(id="second", messageStart=start))
            reopened = Session(Path(folder))
            for kind in ("resume", "amend", "answer", "answer_amend"):
                self.assertEqual(task_input.message_start(reopened.load(), kind), start)
            reopened.task_history_start = start
            reopened.save(msgs=[*new, dict(role="assistant", content="current")])
            self.assertEqual(reopened.load()["msgs"], original + new + [dict(role="assistant", content="current")])

    def test_old_checkpoint_with_prepared_user_text_can_find_the_current_goal(self):
        checkpoint = dict(msgs=[dict(role="user", content="old"), dict(role="assistant", content="done"),
                                dict(role="user", content="audit\n\nResource bundle", context_spans=[["task",0,5]])],
                          effectiveRequirements=dict(entries=[dict(kind="goal", text="audit")]))
        self.assertEqual(task_input.message_start(checkpoint, "resume"), 2)

    def test_repeated_legacy_goal_uses_the_run_event_and_its_original_call(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            rows = [dict(seq=1, act='user', text='inspect this', run_id='latest'),
                    dict(seq=2, act='tool', role='executor', tool='inspect', action_id='old',
                         call_id='second-call', run_id='latest')]
            (directory/'events.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in rows))
            checkpoint = dict(run_id='latest', taskInput=dict(id='latest'), msgs=[
                dict(role='user', content='inspect this'),
                dict(role='assistant', tool_calls=[dict(id='first-call')]),
                dict(role='user', content='inspect this'),
                dict(role='assistant', tool_calls=[dict(id='second-call')])],
                effectiveRequirements=dict(entries=[dict(kind='goal', text='inspect this')]))
            self.assertEqual(task_input.message_start(checkpoint, 'resume', directory), 2)
            (directory/'events.jsonl').unlink()
            self.assertEqual(task_input.message_start(checkpoint, 'resume', directory), 4)

    def test_resume_rechecks_read_identity_without_resetting_for_added_resources(self):
        with tempfile.TemporaryDirectory() as directory:
            saved = dict(taskInput=dict(readBindings={"repo": "v1"}), msgs=[])
            self.assertFalse(task_input.lost_reads(saved, {"repo": "v1", "extra": "v2"}, Path(directory), 0))
            self.assertTrue(task_input.lost_reads(saved, {"repo": "v2"}, Path(directory), 0))
            self.assertTrue(task_input.lost_reads(saved, {}, Path(directory), 0))

    def test_identical_input_obeys_the_actual_question_and_steering_state(self):
        previous = dict(run_id="old-run", status="paused", msgs=[{}, {}, {}],
                        taskInput=dict(id="logical", revision=2, messageStart=1))
        for kind, state, steering, first in (
                ("new", {}, False, 0), ("resume", previous, False, 1),
                ("amend", previous, True, 1),
                ("answer", dict(previous, status="needs_human", waitQuestion="which tag?"), False, 1)):
            with self.subTest(kind=kind):
                actual = task_input.classify("继续", state, steering=steering)
                bound = task_input.bind(state.get("taskInput", {}), actual, "next-run", state)
                self.assertEqual((actual, task_input.message_start(state, actual)), (kind, first))
                self.assertEqual(bound["id"], "next-run" if kind == "new" else "logical")
                self.assertEqual(bound["questionRunId"], "old-run" if kind == "answer" else None)
