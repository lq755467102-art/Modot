"""One user submission survives budgeting, projection and auxiliary summarization as a unit."""
import unittest
from unittest.mock import patch

from loop import context_manager as C, context_view as V, image_evidence, input_budget as B
from loop.context_inspection import request_owner
from loop.provider import Msg
from tools.context_budget_fixture import request_fixture


class SubmissionBudgetTests(unittest.TestCase):
    def setUp(self):
        self.session, self.provider, self.events = request_fixture(self)
        self.question = Msg("user", "Compare both pictures in order.", context_parts=(("task", "Compare both pictures in order."),))
        self.photos = [image_evidence.model_message(dict(name=name, data_url=url), name, "run")
                       for name, url in (("1.png", "data:image/png;base64,QQ=="),
                                         ("2.png", "data:image/png;base64,Qg=="))]

    def grouped(self, before=(), after=()):
        return image_evidence.for_submission([*before, self.question, *after], self.question, self.photos)

    def test_exact_text_and_ordered_images_share_one_user_item_on_both_entries(self):
        prior = [Msg("user", "previous"), Msg("assistant", "previous reply")]
        agreement = Msg("user", "runtime agreement", context_parts=(("agreement", "runtime agreement"),))
        for history in ([], prior):
            with self.subTest(history=len(history)):
                rows = self.grouped(history, [agreement])
                self.assertEqual(len(rows), len(history) + 2)
                content = rows[len(history)].content
                self.assertEqual(content[0], dict(type="text", text=self.question.content))
                self.assertEqual([p["image_url"]["url"] for p in content if p["type"] == "image_url"],
                                 [p.content[-1]["image_url"]["url"] for p in self.photos])
                self.assertIs(rows[-1], agreement)
                self.assertIsInstance(self.question.dump()["content"], str)
                wire = self.provider._body(rows, [])
                self.assertEqual(image_evidence.request_image_actions(wire), {"1.png", "2.png"})
                self.assertNotIn("imageAction", str(dict(wire)))
                self.assertIn(len(history), B.required_rows(wire))
                self.assertNotIn("currentSend", str(dict(wire)))
        with self.assertRaises(C.ContextBlocked):
            image_evidence.for_submission(prior, self.question, self.photos)

    def test_complete_current_exceeds_hard_cap_before_auxiliary_or_answer_request(self):
        C.bind_capacity(self.session, self.provider, 12500)
        with request_owner(self.session, "executor"), patch.object(self.provider, "_once") as network:
            with self.assertRaises(C.ContextBlocked):
                self.provider.chat(self.grouped([Msg("system", "rules"), Msg("assistant", "old " * 8000)]), [])
        network.assert_not_called()
        blocked = next(e for e in self.events if e["act"] == "context_blocked")
        self.assertEqual(blocked["inputBudget"]["reason"], "current")
        self.assertIn("2 张图片", blocked["reason"])
        self.assertFalse(any(e["act"] == "context_compaction" for e in self.events))

    def test_summary_excludes_current_send_even_with_recent_tool_history(self):
        C.bind_capacity(self.session, self.provider, 60000)
        old = [Msg("system","rules"), Msg("user","old question"), Msg("assistant","earlier work " * 1800)]
        tail = [Msg("assistant","newer completed work " * 1350), Msg("assistant","tail")]
        rows = self.grouped(old, tail)
        captured = []
        from loop.provider import Reply
        def summarize(messages, *args, **kwargs):
            captured.append([m.to_dict() for m in messages])
            return Reply(text="Earlier work was verified.", finish_reason="stop")
        with request_owner(self.session, "executor"), patch.object(self.provider, "chat", side_effect=summarize):
            source = self.provider._body(rows, [])
            projected = V.project(self.session, "executor", source)
            result = V.summarize(self.provider, self.session, "executor", projected, None, 60000, retain_tokens=1)
        self.assertEqual(len(captured), 1)
        self.assertNotIn(self.question.content, str(captured[0]))
        self.assertNotIn("image_url", str(captured[0]))
        self.assertEqual([m for m in result["messages"] if isinstance(m.get("content"),list)],
                         [source["messages"][len(old)]])

    def test_checkpoint_pointer_alone_does_not_recompact_an_admitted_send(self):
        from loop import history_compaction as H

        pointer = Msg("assistant", "Summary of earlier work.", context_parts=(("contextView","work-checkpoint"),))
        wire = self.provider._body(self.grouped([Msg("system","rules"), pointer]), [])
        policy = {"threshold": 14000, "apiWindow": 21000, "reservedOutput": 256}
        tokens = H.measured(self.provider, wire, 20000)["tokens"]
        self.assertGreater(tokens, policy["threshold"])
        self.assertTrue(B.fits_after_compaction(wire, tokens, policy, 20000))
        self.assertFalse(B.fits_after_compaction(wire, 21000, policy, 20000))
        old = self.provider._body(self.grouped([pointer, Msg("assistant","additional historical evidence")]), [])
        self.assertFalse(B.fits_after_compaction(old, tokens, policy, 20000))

    def test_selected_document_reference_survives_and_access_is_rechecked_before_send(self):
        from loop import workbench

        rid = "c" * 32
        doc = Msg("user", "请根据所附说明回答。\n当前用户消息实际附带的文件：spec.md")
        rows = image_evidence.for_submission([doc], doc, [], [rid])
        self.assertEqual(rows[0].content, doc.content)
        with request_owner(self.session, "executor"):
            body = self.provider._body(rows, [])
            self.assertIn(0, B.required_rows(body))
            self.assertNotIn(rid, str(dict(body)))
            denied = workbench.Problem("attachment_unavailable","已撤权",409)
            with patch("loop.attachments.message_attachments", side_effect=denied) as grants:
                with self.assertRaisesRegex(C.ContextBlocked, "附件已不可读取"):
                    self.provider._request(body)
                grants.assert_called_once_with(self.session.dir.name, [rid])

    def test_later_completed_work_can_still_be_summarized_while_current_images_stay_intact(self):
        from loop.provider import Reply

        pending = Msg("assistant", "recent tail")
        rows = self.grouped([Msg("system","rules")], [Msg("assistant","completed evidence " * 1400), pending])
        with request_owner(self.session, "executor"), patch.object(
                self.provider, "chat", return_value=Reply(text="Earlier work checked.", finish_reason="stop")) as auxiliary:
            source = self.provider._body(rows, [])
            projected = V.project(self.session, "executor", source)
            result = V.summarize(self.provider, self.session, "executor", projected, None, 60000, retain_tokens=1)
        summary_input = [m.to_dict() for m in auxiliary.call_args.args[0]]
        self.assertIn("completed evidence", str(summary_input))
        self.assertNotIn("image_url", str(summary_input))
        self.assertNotIn(self.question.content, str(summary_input))
        self.assertIn(source["messages"][1], result["messages"])
        self.assertIn(pending.to_dict(), result["messages"])
