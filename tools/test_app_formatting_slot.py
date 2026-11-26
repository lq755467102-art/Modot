"""Cases that require multiple live windows, stale receipts or incomplete transfers."""

import base64
from hashlib import sha256
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from loop import app_formatting as fmt, vsix_upload, workbench as W


class FormatDrafts(unittest.TestCase):
    def setUp(self):
        temp = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(temp).resolve()
        (self.root / "sample.js").write_bytes(b"const saved=1;\n")
        self.pkg = dict(
            id="pkg",
            revision=3,
            remoteEnabled=True,
            writeEnabled=True,
            applicationBinding=dict(parentId="other-editor", slot=fmt.SLOT, extensionId="x.formatter"),
        )
        self.enterContext(patch.dict(fmt._editors, clear=True))
        self.enterContext(patch.object(fmt.plugins, "state", return_value={"plugins": {"pkg": self.pkg}}))
        self.enterContext(
            patch.object(
                fmt.project_workroom,
                "context",
                return_value=dict(
                    status="ready", scopeRevision="scope", roots=[dict(resourceId="resource", path=temp)]
                ),
            )
        )
        self.enterContext(patch.object(fmt.extension_host, "checkout", return_value=self.root))
        self.enterContext(patch.object(fmt, "runtime", return_value=True))
        self.body = dict(
            package="pkg",
            revision=3,
            conversation="thread",
            project="project",
            resource="resource",
            scope="scope",
            path="sample.js",
            language="javascript",
            seq=1,
        )
        self.editor = "1" * 32

    def route(self, op, body=None):
        return fmt.editor_route(
            "other-editor", [self.editor, op], "GET" if body is None else "POST", body or {}
        )

    def test_only_the_live_owner_can_deliver_text_after_approval(self):
        token = self.route("document", self.body)["documentId"]
        self.assertNotIn("text", fmt._editors[self.editor])
        with patch.object(fmt.extension_host, "run") as launch:
            job = self.route("preview", {"documentId": token})
            done = fmt._editors[self.editor]["job"]["done"]
            self.assertTrue(self.route("status")["needsDraft"])
            self.route("document", dict(self.body, seq=2))
            with self.assertRaises(W.Problem):
                self.route("source", dict(documentId=token, previewId=job["previewId"], text="old draft"))
            self.assertTrue(done.wait(3))
            launch.assert_not_called()

    def test_returning_to_the_same_text_cannot_resurrect_a_receipt(self):
        token = self.route("document", self.body)["documentId"]
        with patch.object(
            fmt.extension_host,
            "run",
            return_value=dict(text="const a = 1;\n", edits=1, provider="x.formatter", language="javascript"),
        ):
            job = self.route("preview", {"documentId": token})
            identity = dict(documentId=token, previewId=job["previewId"])
            self.route("source", dict(identity, text="const a=1;\n"))
            self.assertTrue(fmt._editors[self.editor]["job"]["done"].wait(3))
            self.assertIn("+const a = 1;", self.route("review")["diff"])
            for seq in (2, 3):
                self.route("document", dict(self.body, seq=seq))
            with self.assertRaises(W.Problem):
                self.route("accept", identity)
        self.assertEqual((self.root / "sample.js").read_bytes(), b"const saved=1;\n")

    def test_ambiguous_windows_and_other_conversations_are_not_selected(self):
        self.route("document", self.body)
        owner = fmt.Client("thread", self.pkg, lambda: None)
        self.assertEqual(owner.document()["editor"], self.editor)
        with self.assertRaises(W.Problem):
            fmt.Client("another-thread", self.pkg, lambda: None).document()
        self.editor = "2" * 32
        self.route("document", self.body)
        with self.assertRaises(W.Problem):
            owner.document()
        self.route("close", {})
        self.assertEqual(owner.document()["editor"], "1" * 32)

    def test_unified_diff_records_final_newline_changes(self):
        delta = fmt.unified("const x = 1;", "const x = 1;\n", "sample.js")
        self.assertIn("\\ No newline at end of file", delta)
        self.assertEqual(fmt.unified("", "", "sample.js"), "")

    def test_reordered_or_incomplete_original_cannot_be_installed(self):
        with patch.dict(vsix_upload._uploads, clear=True):
            raw = b"a" * vsix_upload.CHUNK + b"b"
            digest = sha256(raw).hexdigest()

            def put(**body):
                return vsix_upload.transfer("other-editor", fmt, body)

            token = put(size=len(raw), sha256=digest)["upload"]
            with self.assertRaises(W.Problem):
                put(upload=token, index=1, chunk="Yg==")
            put(upload=token, index=0, chunk=base64.b64encode(raw[:-1]).decode())
            with self.assertRaises(W.Problem):
                vsix_upload.finish("other-editor", fmt.SLOT, token, digest)
            put(upload=token, index=1, chunk="Yg==")
            self.assertEqual(vsix_upload.finish("other-editor", fmt.SLOT, token, digest), raw)


if __name__ == "__main__":
    unittest.main()
