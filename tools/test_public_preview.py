"""Publication regressions for attribution and current attachment access."""
import unittest
from unittest.mock import patch

from loop import image_evidence, projects, workbench
from loop.context_surface import ContextSurface
from loop.provider import OpenAICompatProvider
from tools.test_conversations import ConversationFixture


class PublicPreviewTests(ConversationFixture, unittest.TestCase):
    def test_identical_pixels_keep_the_projected_action(self):
        provider = OpenAICompatProvider(base_url="http://127.0.0.1:9/v1", api_key="fixture", model="fixture")
        image = dict(name="panel.png", data_url="data:image/png;base64,AA==", bytes=1)
        reads = [image_evidence.model_message(image, letter * 32, "run") for letter in "ab"]
        def retain_last(_, body, *_args):
            view = ContextSurface(body, empty=True)
            view.append_source(1)
            return view.build()

        answer = {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
        with patch("loop.context_manager.prepare", side_effect=retain_last), patch.object(provider, "_once", return_value=answer):
            sent = provider.chat(reads, []).input_image_actions
        events = []
        image_evidence.confirm_model_images(lambda _, **e: events.append(e), reads, reads, "executor", input_images=sent)
        self.assertEqual([e["action_id"] for e in events], ["b" * 32])

    def test_document_directory_authority_checked_before_return(self):
        _, thread = self.call("POST")
        url = "/" + thread["id"] + "/attachments"
        code, item = self.call("POST", url, {"name": "sheet.csv", "data": "YSxiCjEsMgo="})
        self.assertEqual(code, 200)
        projects.snapshot(thread["id"])
        def finish_read(*_args):
            self.assertEqual(self.call("POST", url, {"remove": item["id"]})[0], 200)
            return {"sheets": [{"name": "Sheet1"}]}
        with patch("loop.documents.read", side_effect=finish_read), self.assertRaises(workbench.Problem) as caught:
            projects.read_resource(thread["id"], item["id"], "locations")
        self.assertEqual(caught.exception.status, 403)
