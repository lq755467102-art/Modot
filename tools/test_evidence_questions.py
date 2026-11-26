import json
import unittest
from tools.test_conversations import ConversationFixture
from loop import workbench as W
from loop import evidence
from loop.session import Session


class EvidenceQuestionTests(ConversationFixture, unittest.TestCase):
    def test_subtask_steps_can_be_questioned_without_continuing_child_execution(self):
        parent, aid = self.source()
        W.change_registry(lambda data:data['threads'][parent['id']].update(subtaskOf=dict(parentId='source-parent',taskId='a'*32,runId='b'*32)))
        code, question = self.call('POST','/'+parent['id']+'/questions',dict(actionId=aid))
        self.assertEqual(code,200,question)
        record = W.registry()['threads'][question['id']]
        self.assertTrue(record['readonlyEvidence'])
        self.assertFalse(record.get('resourceIds'))
        self.assertNotIn('subtaskOf',record)

    def test_question_suppresses_external_hooks_for_first_and_resumed_turn(self):
        import os, sys, subprocess
        from pathlib import Path
        from unittest.mock import patch
        from loop import hooks, loop as L, permissions, projects
        from loop.provider import MockProvider, Msg
        from loop.tools import default_registry

        permissions.route("POST", [], dict(mode="full", expectedRevision=permissions.profile()["revision"], confirm=True))
        parent, aid = self.source()
        _, child = self.call(
            "POST", "/" + parent["id"] + "/questions", {"actionId": aid}
        )
        root = Path(
            W.workspace(projects.conversation(child["id"])["workspaceId"])["path"]
        )
        script = self.root / "hook.py"
        marker = self.root / "hook-marker"
        script.write_text(
            "from pathlib import Path\nPath("
            + repr(str(marker))
            + ").write_text('ran')\n"
        )
        config = self.root / "hooks.json"
        config.write_text(
            json.dumps(
                {
                    "hooks": {
                        event: [
                            {
                                "command": subprocess.list2cmdline(
                                    [sys.executable, str(script)]
                                )
                            }
                        ]
                        for event in hooks.EVENTS
                    }
                }
            )
        )
        session = Session(self.root / child["id"])
        with patch.dict(
            os.environ, {"HARNESS_HOOKS": str(config), "HARNESS_ROOT": str(root)}
        ):
            for event in hooks.EVENTS:
                self.assertEqual(
                    hooks.run(event, {"session": str(session.dir)}, root), []
                )
            for turn in range(2):
                saved = (
                    [Msg.load(m) for m in session.load().get("msgs", [])]
                    if turn
                    else None
                )
                L.run(
                    "explain",
                    MockProvider(
                        [
                            {"tool": "read_selected_evidence", "args": {}},
                            {"text": "answer"},
                        ]
                    ),
                    default_registry(root),
                    lambda _: {"ok": True},
                    session=session,
                    resume_msgs=saved,
                    max_turns=2,
                )
            self.assertFalse(marker.exists())
        with patch.dict(
            os.environ, {"HARNESS_HOOKS": str(config), "HARNESS_ROOT": str(self.root)}
        ):
            rows = hooks.run("Stop", {"session": "ordinary"}, self.root)
            self.assertTrue(rows[0]["ok"], rows)
            self.assertTrue(marker.exists())

    def test_deleted_unrelated_question_does_not_break_tools_and_runner_is_fixed(self):
        from types import SimpleNamespace
        from loop.tools import default_registry
        from loop import evidence_questions, projects

        parent, aid = self.source()
        _, child = self.call(
            "POST", "/" + parent["id"] + "/questions", {"actionId": aid}
        )
        record = projects.conversation(child["id"])
        root = W.workspace(record["workspaceId"])["path"]
        args = SimpleNamespace(
            plan_first=True, live=True, draft_contract=True, policy="trusted"
        )
        self.assertTrue(evidence_questions.restrict_runner(root, args))
        self.assertFalse(args.plan_first or args.live or args.draft_contract)
        self.assertTrue(
            args.no_benni and args.no_contract
        )
        self.assertEqual(args.policy, "readonly")
        W.change_registry(
            lambda data: data["workspaces"][record["workspaceId"]].update(
                path=root + "-missing"
            )
        )
        parent_root = W.workspace(projects.conversation(parent["id"])["workspaceId"])[
            "path"
        ]
        self.assertIn("publish_artifact", default_registry(parent_root).names())

    def test_parent_can_append_unrelated_events_while_question_is_created(self):
        from unittest.mock import patch

        parent, aid = self.source()
        original = evidence.output

        def advancing(*args, **kwargs):
            page = original(*args, **kwargs)
            with (self.root / parent["id"] / "events.jsonl").open("a") as stream:
                stream.write(
                    json.dumps(dict(seq=2, act="note", text="parent continues")) + "\n"
                )
            return page

        with patch.object(evidence, "output", side_effect=advancing):
            code, child = self.call(
                "POST", "/" + parent["id"] + "/questions", {"actionId": aid}
            )
        self.assertEqual(code, 200, child)

    def source(self, payload=None):
        _, thread = self.call("POST")
        directory = self.root / thread["id"]
        aid = "a" * 32
        meta = evidence.capture(
            Session(directory), aid, payload or {"answer": "only selected output"}
        )
        (directory / "events.jsonl").write_text(
            json.dumps(
                dict(
                    seq=1,
                    run_id="run",
                    act="action",
                    action_id=aid,
                    tool="check",
                    role="executor",
                    state="completed",
                    outputEvidence=meta,
                )
            )
            + "\n"
        )
        return thread, aid

    def test_question_freezes_evidence_without_touching_parent_and_has_no_write_tools(
        self,
    ):
        from loop.tools import default_registry
        from loop import evidence_questions, projects

        parent, aid = self.source()
        original = (self.root / parent["id"] / "events.jsonl").read_bytes()
        code, child = self.call(
            "POST", "/" + parent["id"] + "/questions", {"actionId": aid}
        )
        self.assertEqual(code, 200, child)
        record = projects.conversation(child["id"])
        root = W.workspace(record["workspaceId"])["path"]
        reg = default_registry(root)
        self.assertEqual(reg.names(), ["read_selected_evidence"])
        from loop import loop as L
        from loop.provider import MockProvider
        from loop.policy import Policy

        session = Session(self.root / child["id"])
        L.run(
            "explain",
            MockProvider(
                [
                    {
                        "tool": "publish_artifact",
                        "args": {"title": "forbidden", "text": "write"},
                    },
                    {
                        "tool": "write_file",
                        "args": {"path": "forbidden.txt", "text": "write"},
                    },
                    {"text": "I cannot write"},
                ]
            ),
            reg,
            lambda _: {"ok": True},
            session=session,
            policy=Policy("readonly", root=root),
            max_turns=3,
        )
        self.assertFalse(projects.conversation(child["id"]).get("artifacts"))
        self.assertFalse((__import__("pathlib").Path(root) / "forbidden.txt").exists())
        self.assertEqual(
            self.call("POST", "/" + child["id"] + "/scope", {"projectId": "other"})[0],
            403,
        )
        self.assertEqual(
            self.call("POST", "/" + child["id"] + "/attachments", {})[0], 403
        )
        self.assertEqual(
            self.call("POST", "/" + child["id"] + "/artifacts", {"title": "forbidden"})[
                0
            ],
            403,
        )
        self.assertEqual(
            self.call(
                "POST",
                "/" + child["id"] + "/send",
                {"text": "why", "policy": "trusted"},
            )[1]["policy"],
            "readonly",
        )
        self.assertEqual(
            (self.root / parent["id"] / "events.jsonl").read_bytes(), original
        )
        (self.root / parent["id"] / "events.jsonl").write_text(
            original.decode()
            + json.dumps(dict(seq=2, act="note", text="new version"))
            + "\n"
        )
        self.assertIn("only selected output", evidence_questions.read(root)["text"])
        self.assertEqual(projects.scope(child["id"])["resources"], [])

    def test_missing_and_oversized_evidence_rejected_before_new_conversation(self):
        parent, aid = self.source({"text": "x" * 40000})
        count = len(W.registry()["threads"])
        self.assertEqual(
            self.call("POST", "/" + parent["id"] + "/questions", {"actionId": aid})[0],
            413,
        )
        self.assertEqual(len(W.registry()["threads"]), count)
        self.assertEqual(
            self.call(
                "POST", "/" + parent["id"] + "/questions", {"actionId": "missing"}
            )[0],
            404,
        )
        parent, aid = self.source()
        (self.root / parent["id"] / "evidence-output" / (aid + ".json")).unlink()
        self.assertEqual(
            self.call("POST", "/" + parent["id"] + "/questions", {"actionId": aid})[0],
            410,
        )
