import unittest
from io import BytesIO
from unittest.mock import patch

from PIL import Image

from loop import image_evidence as image
from loop.provider import Msg, OpenAICompatProvider


class ImageEventTests(unittest.TestCase):
    def test_confirmation_is_by_action_across_resume_and_duplicate_pixels(self):
        original = dict(name="layout.png", data_url="data:image/png;base64,AA==", bytes=1)
        first = image.model_message(original, "a" * 32, "previous")
        second = image.model_message(original, "b" * 32, "current")
        resumed = Msg.load(second.dump())
        self.assertNotIn("image_action", resumed.to_dict())
        events = []

        def emit(act, **event):
            events.append(dict(act=act, **event))

        image.confirm_model_images(emit, [resumed], [first, second], "executor")
        self.assertEqual(events, [dict(act="image", action_id="b" * 32, run_id="current", role="executor", bytes=1)])
        self.assertFalse(first.image_action.get("confirmed", False))
        self.assertTrue(second.image_action["confirmed"])
        image.confirm_model_images(emit, [second], [second], "executor")
        self.assertEqual(len(events), 1)

    def test_png_needs_valid_container_and_pixel_data(self):
        data = BytesIO()
        Image.new("RGB", (10, 8), "red").save(data, format="PNG")
        raw = data.getvalue()
        self.assertEqual(image.decode(raw, "PNG"), (10, 8))
        with self.assertRaises((OSError, ValueError, SyntaxError)):
            image.decode(raw[:-12], "PNG")

    def test_image_receipt_requires_media_in_the_successful_request(self):
        provider = OpenAICompatProvider(
            base_url="http://127.0.0.1:9/v1", api_key="fixture-only", model="fixture",
        )
        original = dict(name="layout.png", data_url="data:image/png;base64,AA==", bytes=1)
        message = image.model_message(original, "a" * 32, "current")
        wire_reply = {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
        with patch.object(provider, "_once", return_value=wire_reply):
            with patch("loop.context_manager.prepare", side_effect=lambda _, body, *_args: dict(body, messages=[])):
                omitted = provider.chat([message], [])
            included = provider.chat([message], [])

        events = []
        def emit(act, **event):
            events.append(dict(act=act, **event))

        image.confirm_model_images(
            emit, [message], [message], "executor", input_images=omitted.input_image_actions,
        )
        self.assertEqual(events, [])
        image.confirm_model_images(
            emit, [message], [message], "executor", input_images=included.input_image_actions,
        )
        self.assertEqual([row["action_id"] for row in events], ["a" * 32])
