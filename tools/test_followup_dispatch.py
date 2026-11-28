"""Ordered follow-up dispatch at an idle, archived, or recovered session boundary."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bin import server
from loop import operations
from tools.test_conversations import ConversationFixture


class FollowupDispatch(ConversationFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        _, thread = self.call("POST", body={"title": "Follow-up dispatch"})
        self.sid = thread["id"]
        self.directory = self.sv._resolve_sid(self.sid)
        self.path = "/api/v2/conversations/threads/" + self.sid + "/send"
        self.active = object()
        self.launched = []
        self.waiters = []
        for obj, changes in (
            (server, {"APP": self.root, "ROOT": self.root, "RUNS": self.root,
                      "_active_proc": lambda _: self.active, "_queue_after": self.hold,
                      "_resolve_sid": self.sv._resolve_sid, "_continue_unlocked": self.start}),
            (self.sv, {"_active_proc": lambda _: self.active, "_queue_pending": server._queue_pending,
                       "_continue": server._continue}),
        ):
            for name, value in changes.items():
                replacement = patch.object(obj, name, value, create=True)
                replacement.start()
                self.addCleanup(replacement.stop)

    def hold(self, directory, process, callback, **kwargs):
        self.waiters.append(callback)
        return True

    def start(self, directory, text, *args, **kwargs):
        self.assertIsNone(self.active)
        run = f"run-{len(self.launched) + 1}"
        self.launched.append((text.splitlines()[0], run))
        self.active = SimpleNamespace(pid=1234)
        return 200, {"runId": run}

    def send(self, ident, text):
        body = {"text": text, "mode": "queued"}
        fresh, old = operations.claim(self.root, ident, self.path, body)
        if not fresh:
            return old["response"]
        status, result = self.call("POST", "/" + self.sid + "/send", dict(body, _request_id=ident))
        operations.finish(self.root, ident, status, result)
        self.assertEqual(status, 200, result)
        return result

    def receipt(self, ident):
        return operations.get(self.root, ident)

    def archive(self, value):
        code, response = self.call("POST", "/" + self.sid + "/archive", {"archived": value})
        self.assertEqual(code, 200, response)

    def test_new_input_cannot_pass_accepted_input_in_idle_gap(self):
        a, b, c = "send-earlier-A", "send-later-B", "send-latest-C"
        self.assertTrue(self.send(a, "A")["queued"])
        self.active = None
        self.assertTrue(self.send(b, "B")["queued"])
        self.assertTrue(self.send(c, "B")["queued"])  # Two deliberate sends of the same text.
        self.assertTrue(self.send(b, "B")["queued"])  # Transport retry of B.
        self.assertEqual(self.launched, [])
        for index, ident in enumerate((a, b, c), 1):
            self.active = None
            server._run_pending(self.directory)
            server._run_pending(self.directory)  # Another wake while the new process is active.
            receipt = self.receipt(ident)
            self.assertEqual(receipt["state"], "completed")
            self.assertEqual(receipt["response"]["out"]["runId"], f"run-{index}")
        self.active = None
        for wake in list(self.waiters):
            wake()
        self.assertEqual([text for text, _ in self.launched], ["A", "B", "B"])
        self.assertEqual(self.send(b, "B")["out"]["runId"], "run-2")
        self.assertFalse(operations.waiting_inputs(self.directory))

    def test_archive_settles_each_original_receipt_before_restore_and_recovery(self):
        messages = {"send-archA": "A" * 600 + "完整结尾", "send-archB": "B"}
        for ident, text in messages.items():
            self.assertEqual(self.send(ident, text)["messageText"], text)
        self.active = None
        self.archive(True)
        for ident in ("send-archA", "send-archB"):
            receipt = self.receipt(ident)
            self.assertEqual(receipt["state"], "not_dispatched")
            self.assertEqual(receipt["response"]["out"]["code"], "archived_conversation")
            self.assertEqual(receipt["response"]["messageText"], messages[ident])
        self.archive(False)
        with patch.object(server, "_recover_one", side_effect=lambda d: server._run_pending(d)):
            server._recover_pending()
        for wake in list(self.waiters):
            wake()
        self.assertEqual(self.launched, [])
        self.assertEqual(self.send("send-archA", messages["send-archA"])["out"]["code"], "archived_conversation")
        self.assertFalse(operations.waiting_inputs(self.directory))
        self.assertEqual(self.send("send-fresh", "fresh")["runId"], "run-1")

    def test_restart_after_claiming_an_already_cancelled_message(self):
        self.send("send-cancel", "cancelled before claim")
        self.active = None
        self.archive(True)
        self.assertTrue(server._claim_pending(self.directory)["cancelled"])
        self.assertFalse((self.directory / server.PENDING_RESULT).exists())
        server._recover_pending()
        self.assertEqual(self.receipt("send-cancel")["state"], "not_dispatched")
        self.archive(False)
        server._run_pending(self.directory)
        self.assertEqual(self.launched, [])

    def test_tail_survives_restart_and_uncertain_claim_is_not_replayed(self):
        self.assertTrue(self.send("old-head", "A")["queued"])
        self.assertTrue(self.send("next-tail", "B")["queued"])
        self.active = None
        claimed = server._claim_pending(self.directory)
        self.assertEqual(claimed["request"]["text"], "A")
        # The dispatcher owned the claim; recovery cannot assume it never launched.
        with patch.object(server, "_recover_one", side_effect=lambda d: server._run_pending(d)):
            server._recover_pending()
            self.assertEqual(self.receipt("old-head")["state"], "unknown")
            self.assertEqual(self.receipt("next-tail")["state"], "queued")
            self.assertEqual(self.launched, [])
        claimed["dispatcher"] = {}
        (self.directory / server.PENDING_CLAIMED).write_text(json.dumps(claimed), encoding="utf-8")
        with patch.object(server, "_recover_one", return_value=True) as recover:
            self.assertEqual(server._acknowledge_pending(self.directory)[0], 200)
            recover.assert_called_once_with(self.directory)
        server._run_pending(self.directory)
        self.assertEqual(self.launched, [("B", "run-1")])
        self.assertEqual(self.receipt("old-head")["state"], "unknown")
