"""Timestamped video attachments through scoped reads and retained image evidence."""

import base64
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools.test_conversations import ConversationFixture


def encode_video(file_format, images, rate=4):
    import av

    output = BytesIO()
    codec = "libx264" if file_format == "mp4" else "libvpx-vp9"
    with av.open(output, "w", format=file_format) as container:
        stream = container.add_stream(codec, rate=rate)
        stream.width, stream.height = images[0].size
        stream.pix_fmt = "yuv420p"
        for image in images:
            for packet in stream.encode(av.VideoFrame.from_image(image)):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    return output.getvalue()


class VideoAttachmentTests(ConversationFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        from loop import projects, workbench as W
        from loop.tools import default_registry

        self.projects = projects
        self.W = W
        _, thread = self.call("POST")
        self.ident = thread["id"]
        self.uploads = f"/{self.ident}/attachments"
        workspace = W.workspace(W.registry()["threads"][self.ident]["workspaceId"])
        self.read = default_registry(Path(workspace["path"])).get("read_project_resource")

    def upload(self, name, data):
        return self.call("POST", self.uploads, {"name": name, "data": base64.b64encode(data).decode()})

    def test_formats_intervals_and_revoking_original_frames(self):
        from PIL import Image
        from loop import image_evidence

        blue = Image.new("RGB", (80, 48), (0, 0, 220))
        red = Image.new("RGB", (80, 48), (220, 0, 0))
        for file_format in ("mp4", "webm"):
            with self.subTest(file_format=file_format):
                name = f"build.{file_format}"
                status, item = self.upload(name, encode_video(file_format, [blue] * 4 + [red] * 4))
                self.assertEqual((status, item["mime"]), (200, f"video/{file_format}"))
                self.projects.snapshot(self.ident)
                args = {"resource_id": item["id"]}
                metadata = self.read(args)
                self.assertEqual(metadata.get("readKind"), "video_metadata", metadata)
                self.assertAlmostEqual(metadata["durationSeconds"], 2.0, places=2)
                self.assertNotIn("image", metadata)
                self.assertEqual(self.read(dict(args, video={"start_seconds": 3}))["code"], "video_range_outside")

                query = dict(args, video={"start_seconds": 0.3, "end_seconds": 1.3, "frame_count": 2})
                result = self.read(query)
                self.assertEqual(result.get("readKind"), "video_frames", result)
                self.assertEqual(result["frames"], [
                    {"requestedSeconds": 0.3, "timestampSeconds": 0.25},
                    {"requestedSeconds": 1.3, "timestampSeconds": 1.25},
                ])
                with Image.open(BytesIO(base64.b64decode(result["image"]["data_url"].split(",")[1]))) as sheet:
                    self.assertGreater(sheet.getpixel((240, 135))[2], 180)
                    self.assertGreater(sheet.getpixel((720, 135))[0], 180)

                store = self.root / self.ident
                image_evidence.preserve(SimpleNamespace(dir=store), result, "read_project_resource")
                self.assertTrue(result["imageView"]["retained"])
                image_evidence.authorize(self.ident, result["imageView"])
                self.assertEqual(self.call("POST", self.uploads, {"remove": item["id"]})[0], 200)
                self.assertEqual(self.read(query)["code"], "resource_denied")
                with self.assertRaises(self.W.Problem) as denied:
                    image_evidence.authorize(self.ident, result["imageView"])
                self.assertEqual(denied.exception.code, "resource_denied")
                self.assertIn(name, self.projects.prepare_turn(self.root / self.ident, "再看原片"))

    def test_range_validation_and_attachment_identity(self):
        from PIL import Image

        encoded = encode_video("mp4", [Image.new("RGB", (80, 48), "orange")] * 4)
        status, invalid = self.upload("renamed.webm", encoded)
        self.assertEqual((status, invalid["code"]), (400, "invalid_video"))
        _, item = self.upload("build.mp4", encoded)
        _, text = self.upload("build.txt", b"plain text")
        self.projects.snapshot(self.ident)
        args = {"resource_id": item["id"]}
        time = {"start_seconds": 0.5}
        for value in ([], {"start_seconds": True}, {"start_seconds": -1},
                      {"start_seconds": float("inf")}, {"start_seconds": 1, "end_seconds": 0},
                      dict(time, frame_count=5)):
            self.assertEqual(self.read(dict(args, video=value))["code"], "invalid_video_range")
        self.assertEqual(self.read(dict(args, video=time, offset=3))["code"], "invalid_cursor")
        self.assertEqual(self.read(dict(args, video=time, line=1))["code"], "invalid_video_range")
        self.assertEqual(self.read(dict(args, video=time, path="elsewhere.mp4"))["code"], "attachment_file_mismatch")
        self.assertEqual(self.read({"resource_id": text["id"], "video": time})["code"], "unsupported_video")
        (Path(item["path"]) / item["name"]).write_bytes(encoded + b"x")
        self.assertEqual(self.read(dict(args, video=time))["code"], "parser_source")

    def test_revocation_while_decoding_discards_result(self):
        from PIL import Image
        from loop import video_frames

        _, item = self.upload("build.webm", encode_video("webm", [Image.new("RGB", (80, 48), "red")] * 4))
        self.projects.snapshot(self.ident)

        def decode(_request):
            self.assertEqual(self.call("POST", self.uploads, {"remove": item["id"]})[0], 200)
            return {"ok": True}

        with patch.object(video_frames, "_decode", side_effect=decode):
            result = self.read({"resource_id": item["id"], "video": {"start_seconds": 0.5}})
        self.assertEqual(result["code"], "resource_denied")
        self.assertNotIn("image", result)


if __name__ == "__main__":
    unittest.main()
