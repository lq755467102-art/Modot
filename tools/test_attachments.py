"""Conversation-local imports via the public API and scoped resource reader."""

import base64
from io import BytesIO
import unittest
from uuid import uuid4
from tools.test_conversations import ConversationFixture


class AttachmentTests(ConversationFixture, unittest.TestCase):
    def test_removing_two_images_names_both_in_the_next_turn(self):
        from PIL import Image
        from loop import projects

        _, thread = self.call("POST")
        ident = thread["id"]
        directory = self.root / ident
        uploads = f"/{ident}/attachments"
        images = []
        for color in ("green", "blue"):
            stream = BytesIO()
            Image.new("RGB", (5, 3), color).save(stream, format="PNG")
            encoded = base64.b64encode(stream.getvalue()).decode()
            status, item = self.call("POST", uploads, {"name": "image.png", "data": encoded})
            self.assertEqual(status, 200)
            images.append(item)
        _, draft = self.call("POST", uploads, {"name": "draft.txt", "data": "bm90ZQ==", "staged": True})
        self.assertEqual(self.call("POST", uploads, {"remove": draft["id"]})[0], 200)
        projects.prepare_turn(directory, "两张图片")
        self.assertEqual(projects.scope(ident)["lastRun"]["unavailable"], [])
        for item in images:
            self.assertEqual(self.call("POST", uploads, {"remove": item["id"]})[0], 200)
        self.assertEqual(self.call("GET", uploads)[1]["attachments"], [])

        prepared = projects.prepare_turn(directory, "现在还能打开吗？")
        self.assertIn("授权已撤销", prepared)
        self.assertNotIn(draft["id"], prepared)
        _, other = self.call("POST")
        unrelated = projects.prepare_turn(self.root / other["id"], "有什么资料？")
        scoped = projects.scope(ident)["lastRun"]
        self.assertEqual(scoped["resources"], [])
        self.assertEqual({row["id"] for row in scoped["unavailable"]}, {item["id"] for item in images})
        for item in images:
            self.assertIn(item["id"], prepared)
            self.assertNotIn(item["id"], unrelated)
            self.assertNotIn(item["path"], prepared)
            self.assertNotIn(item["hash"], prepared)

    def test_read_tool_distinguishes_single_attachment_references_from_directories(self):
        from pathlib import Path
        from PIL import Image
        from loop import projects, workbench as W
        from loop.tools import default_registry

        checkout = self.root / "checkout"
        checkout.mkdir()
        (checkout / "project.txt").write_text("project", encoding="utf-8")
        status, project = W.handle(
            "POST", "/api/v2/projects", {},
            {"path": str(checkout), "confirm": True}, self.sv,
        )
        self.assertEqual(status, 200, project)
        _, thread = self.call("POST", body={"projectId": project["id"]})
        ident = thread["id"]
        uploads = f"/{ident}/attachments"
        images = []
        for color in ("green", "blue"):
            stream = BytesIO()
            Image.new("RGB", (5, 3), color).save(stream, format="PNG")
            encoded = base64.b64encode(stream.getvalue()).decode()
            status, item = self.call("POST", uploads, {"name": "image.png", "data": encoded})
            self.assertEqual(status, 200, item)
            images.append((item, encoded))
        _, note = self.call("POST", uploads, {"name": "note.txt", "data": "bm90ZQ=="})
        projects.snapshot(ident)
        workspace = W.workspace(W.registry()["threads"][ident]["workspaceId"])
        read = default_registry(Path(workspace["path"])).get("read_project_resource")

        for (item, encoded), path in zip(images, ({"path": "."}, {})):
            result = read(dict(resource_id=item["id"], **path))
            self.assertEqual(result.get("readKind"), "image")
            self.assertEqual(result["image"]["data_url"], "data:image/png;base64," + encoded)
            self.assertNotIn("entries", result)
        text = read({"resource_id": note["id"]})
        self.assertEqual((text.get("readKind"), text.get("text")), ("text", "note"))
        folder = next(resource for resource in projects.scope(ident)["resources"]
                      if resource["kind"] == "code")
        listing = read({"resource_id": folder["id"], "path": "."})
        self.assertEqual(listing.get("readKind"), "directory")
        self.assertIn("entries", listing)
        self.assertNotIn("text", listing)

        first = images[0][0]
        (Path(first["path"]) / "other.png").write_bytes(base64.b64decode(images[0][1]))
        mismatch = read({"resource_id": first["id"], "path": "other.png"})
        self.assertEqual(mismatch["code"], "attachment_file_mismatch")
        self.assertEqual(self.call("POST", uploads, {"remove": first["id"]})[0], 200)
        denied = read({"resource_id": first["id"]})
        self.assertEqual(denied.get("code"), "resource_denied")
        self.assertNotIn("image", denied)

    def test_official_deepseek_flash_reads_an_attached_png_without_manual_override(self):
        from unittest.mock import patch
        from PIL import Image
        from loop import models, projects

        _, thread = self.call("POST")
        ident = thread["id"]
        stream = BytesIO()
        Image.new("RGB", (5, 3), "green").save(stream, format="PNG")
        encoded = base64.b64encode(stream.getvalue()).decode()
        status, image = self.call("POST", f"/{ident}/attachments",
                                  {"name": "green.png", "data": encoded})
        self.assertEqual(status, 200)
        with patch.object(models, "resolve", return_value={
            "base_url": "https://api.deepseek.com/v1", "model": "deepseek-flash",
        }):
            projects.snapshot(ident)
            found = projects.read_resource(ident, image["id"], image["name"])
        self.assertEqual(found["image"]["data_url"], "data:image/png;base64," + encoded)

    def test_send_only_resolves_published_attachments_from_that_conversation(self):
        from unittest.mock import patch

        _, thread = self.call("POST")
        _, other = self.call("POST")
        ident = thread["id"]
        payload = {"name": "note.txt", "data": base64.b64encode(b"note").decode()}
        _, active = self.call("POST", f"/{ident}/attachments", payload)
        _, staged = self.call("POST", f"/{ident}/attachments", dict(payload, staged=True))
        _, outside = self.call("POST", f"/{other['id']}/attachments", payload)
        route = f"/{ident}/send"
        with patch.object(self.sv, "_continue", wraps=self.sv._continue) as dispatch:
            for item in (outside, staged):
                status, error = self.call("POST", route, {"text": "inspect", "attachmentIds": [item["id"]]})
                self.assertEqual((status, error["code"]), (409, "attachment_unavailable"))
            self.assertEqual(dispatch.call_count, 0)
            status, _ = self.call("POST", route, {"text": "inspect", "attachmentIds": [active["id"]]})
            self.assertEqual(status, 200)
            self.assertEqual(dispatch.call_args.kwargs["attachment_ids"], [active["id"]])

    def test_cancel_during_upload_publication_leaves_no_readable_attachment(self):
        from concurrent.futures import ThreadPoolExecutor, TimeoutError
        from threading import Event
        from unittest.mock import patch
        from loop import attachments, workbench as W

        _, thread = self.call("POST")
        url = "/" + thread["id"] + "/attachments"
        ident = uuid4().hex
        publishing, release = Event(), Event()
        change_registry = W.change_registry

        def hold_publication(update):
            if update.__name__ == "add":
                publishing.set()
                if not release.wait(5):
                    raise AssertionError("Publication was not released")
            return change_registry(update)

        with patch.object(W, "change_registry", side_effect=hold_publication), ThreadPoolExecutor(2) as pool:
            upload = pool.submit(attachments.route, thread["id"], "POST", dict(
                importId=ident, name="late.txt", data=base64.b64encode(b"late upload").decode()))
            try:
                self.assertTrue(publishing.wait(5))
                cancel = pool.submit(attachments.route, thread["id"], "POST", {"remove": ident})
                try:
                    cancel.result(timeout=.3)
                except TimeoutError:
                    pass
            finally:
                release.set()
            self.assertEqual(upload.result(timeout=5)[0], 200)
            self.assertEqual(cancel.result(timeout=5)[0], 200)
        self.assertEqual(self.call("GET", url)[1]["attachments"], [])
        self.assertNotIn(ident, W.registry()["threads"][thread["id"]]["resourceIds"])

    def test_cancelled_attachment_cannot_reactivate_after_cleanup_error(self):
        from pathlib import Path
        from unittest.mock import patch

        _, thread = self.call("POST")
        url = "/" + thread["id"] + "/attachments"
        body = dict(importId=uuid4().hex, name="note.txt", staged=True,
                    data=base64.b64encode(b"staged upload").decode())
        status, item = self.call("POST", url, body)
        self.assertEqual(status, 200)
        ident = item["id"]
        original = Path.unlink
        payload = Path(item["path"]) / item["file"]

        def fail_payload(path, *args, **kwargs):
            if path == payload:
                raise PermissionError("fixture: attachment held open")
            return original(path, *args, **kwargs)

        with patch.object(Path, "unlink", fail_payload):
            status, error = self.call("POST", url, {"remove": ident})
            self.assertEqual((status, error["code"]), (409, "attachment_cleanup_pending"))
        self.assertEqual(self.call("POST", url, {"activate": ident})[0], 404)
        self.assertEqual(self.call("POST", url, body)[0], 409)
        self.assertEqual(self.call("POST", url, {"remove": ident})[0], 200)
        self.assertFalse(payload.exists())

    def test_cancel_before_upload_reserves_import_identity(self):
        _, thread = self.call("POST")
        url = "/" + thread["id"] + "/attachments"
        ident = uuid4().hex
        for _ in range(2):
            self.assertEqual(self.call("POST", url, {"remove": ident})[0], 200)
        body = dict(importId=ident, staged=True, name="late.txt",
                    data=base64.b64encode(b"late upload").decode())
        status, result = self.call("POST", url, body)
        self.assertEqual((status, result["code"]), (409, "import_unavailable"))
        self.assertEqual(self.call("POST", url, {"activate": ident})[0], 404)
        self.assertEqual(self.call("GET", url)[1]["attachments"], [])
        self.assertEqual(self.call("POST", url, dict(body, importId=uuid4().hex))[0], 200)

    def test_import_is_scoped_versioned_and_validated(self):
        _, a = self.call("POST")
        _, b = self.call("POST")
        upload = "/" + a["id"] + "/attachments"

        def content(name, data):
            return {"name": name, "data": base64.b64encode(data).decode()}

        code, item = self.call("POST", upload, content("note.md", b"first draft"))
        self.assertEqual(code, 200)
        self.assertEqual(item["status"], "ready")
        _, second = self.call("POST", upload, content("note.md", b"second draft"))
        self.assertNotEqual(item["id"], second["id"])
        self.assertNotEqual(item["hash"], second["hash"])
        from loop import projects

        self.assertEqual(
            projects.read_resource(a["id"], item["id"], "note.md")["text"],
            "first draft",
        )
        self.assertFalse(projects.tool_read(self.root, "x", "note.md")["ok"])
        with self.assertRaises(Exception):
            projects.read_resource(b["id"], item["id"], "note.md")
        self.assertEqual(
            self.call("POST", upload, content("fake.png", b"not an image"))[0], 400
        )
        self.assertEqual(
            self.call("POST", upload, content("../outside.txt", b"x"))[0], 400
        )
        self.assertEqual(self.call("POST", upload, content("a.exe", b"x"))[0], 400)
        from PIL import Image

        stream = BytesIO()
        Image.new("RGB", (2, 3), "red").save(stream, format="PNG")
        code, picture = self.call("POST", upload, content("red.png", stream.getvalue()))
        self.assertEqual(code, 200)
        self.assertEqual((picture["width"], picture["height"]), (2, 3))
        from loop import workbench as W
        import uuid

        staged_id = uuid.uuid4().hex
        payload = content("staged.txt", b"cancel me")
        payload.update(importId=staged_id, staged=True)
        code, staged = self.call("POST", upload, payload)
        self.assertEqual(staged["status"], "staged")
        self.assertNotIn(staged_id, W.registry()["threads"][a["id"]]["resourceIds"])
        self.assertEqual(self.call("POST", upload, {"remove": staged_id})[0], 200)
        self.assertFalse(
            (W.home() / "attachments" / a["id"] / staged_id / "staged.txt").exists()
        )
        self.assertNotIn(staged_id, W.registry()["threads"][a["id"]]["attachments"])
        projects.snapshot(a["id"])
        image = projects.read_resource(a["id"], picture["id"], "red.png")
        self.assertTrue(image["image"]["data_url"].startswith("data:image/png;base64,"))
        self.assertEqual(self.call("POST", upload, {"remove": picture["id"]})[0], 200)
        with self.assertRaises(W.Problem):
            projects.read_resource(a["id"], picture["id"], "red.png")

    def test_upstream_image_rejection_is_reported_without_fallback(self):
        from urllib.error import HTTPError
        from unittest.mock import patch
        from loop.provider import OpenAICompatProvider, Msg, ProviderError

        provider = OpenAICompatProvider(base_url="http://127.0.0.1:9/v1", api_key="fixture-only", model="fixture")
        upstream = HTTPError("http://127.0.0.1:9/v1/chat/completions", 400, "invalid", {},
                             BytesIO(b'{"error":{"message":"This model does not support image input"}}'))
        message = Msg("user", [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}])
        with patch.object(provider, "_once", side_effect=upstream) as request:
            with self.assertRaises(ProviderError) as error:
                provider.chat([message], [])
        self.assertEqual(request.call_count, 1)
        self.assertIn(b"data:image/png;base64,AA==", request.call_args.args[0].data)
        self.assertEqual(error.exception.kind, "image_input")
        self.assertFalse(error.exception.can_fallback)


if __name__ == "__main__":
    import unittest

    unittest.main()
