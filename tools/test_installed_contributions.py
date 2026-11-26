"""Exercise installed contribution identities and changes through the workbench API."""
import json
import os
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

from loop import permissions, plugin_contributions, plugin_runtime, plugins, projects, workbench as W
from loop.tools import set_call, reset_call
from tools.test_conversations import ConversationFixture


SAMPLES = Path(__file__).resolve().parents[1] / "examples" / "plugin-catalog"


class InstalledContributions(ConversationFixture, unittest.TestCase):
    def api(self, method, suffix, body=None, query=None, status=200):
        path = "/api/v2/plugins" + ("/" + suffix if suffix else "")
        actual, result = W.handle(method, path, query or {}, body or {}, self.sv)
        self.assertEqual(actual, status, result)
        return result

    def install(self, directory):
        source = {"path": str(directory)}
        preview = self.api("POST", "preview", source)
        return self.api("POST", "install", dict(source, fingerprint=preview["fingerprint"]))

    def replacement(self, old, directory, key="replace-46-test"):
        source = {"path": str(directory)}
        preview = self.api("POST", "preview", source)
        return dict(source, fingerprint=preview["fingerprint"], expectedRevision=old["revision"], requestId=key)

    def icon(self, package, theme="default", status=200):
        return self.api("GET", "contributions/" + package["id"] + "/icon",
                        query={"fingerprint": [package["fingerprint"]], "revision": [str(package["revision"])],
                               "theme": [theme]}, status=status)

    def test_package_icons_follow_install_identity_and_theme(self):
        legacy = self.install(SAMPLES / "legacy-skill")
        other = self.install(SAMPLES / "namesake")
        old = self.install(SAMPLES / "studio-v1")
        self.assertEqual(self.icon(legacy, status=404)["code"], "plugin_icon_missing")
        rows = {p["id"]: p for p in self.api("GET", "contributions")["plugins"]}
        self.assertEqual(rows[old["id"]]["iconThemes"], ["default", "light", "dark"])
        self.assertNotIn("iconThemes", rows[legacy["id"]])
        variants = [self.icon(old, theme) for theme in ("default", "light", "dark")]
        self.assertEqual({(r["width"], r["height"], r["mime"]) for r in variants}, {(32, 32, "image/png")})
        self.assertEqual(len({r["data"] for r in variants}), 3)
        neighbor = self.icon(other, "dark")
        self.assertEqual(neighbor["resolvedTheme"], "default")
        self.assertNotIn(neighbor["data"], {r["data"] for r in variants})

        enabled = self.api("POST", old["id"], dict(expectedRevision=old["revision"], remoteEnabled=True, confirm=True))
        disabled = self.api("POST", old["id"], dict(expectedRevision=enabled["revision"], remoteEnabled=False))
        self.assertEqual(self.icon(enabled, status=409)["code"], "plugin_changed")
        self.assertEqual(self.icon(disabled, "dark")["data"], variants[2]["data"])
        new = self.api("POST", old["id"] + "/replace", self.replacement(disabled, SAMPLES / "studio-v2"))["installed"]
        self.assertEqual(self.icon(disabled, status=410)["code"], "plugin_missing")
        self.assertNotEqual(self.icon(new, "dark")["data"], variants[2]["data"])
        self.assertEqual(self.icon(other, "dark"), neighbor)
        self.api("POST", new["id"], dict(expectedRevision=new["revision"], remove=True))
        self.assertEqual(self.icon(new, status=410)["code"], "plugin_missing")
        reinstalled = self.install(SAMPLES / "studio-v2")
        self.assertEqual(reinstalled["id"], new["id"])
        self.assertEqual(self.icon(new, status=409)["code"], "plugin_changed")
        self.assertEqual(self.icon(reinstalled)["revision"], reinstalled["revision"])

    def test_invalid_optional_icons_do_not_read_unlisted_or_external_resources(self):
        from PIL import Image

        source = Path(shutil.copytree(SAMPLES / "studio-v1", self.root / "invalid-icons"))
        manifest = source / "plugin.json"
        fields = json.loads(manifest.read_text(encoding="utf8"))
        section = fields["extensions"][plugin_contributions.PREVIEW]
        outside = self.root / "outside.png"
        Image.new("RGBA", (8, 8), "white").save(outside)
        (source / "bad.png").write_bytes(b"<svg xmlns='http://www.w3.org/2000/svg'/>")
        Image.new("RGB", (257, 1), "pink").save(source / "large.png")
        frame = Image.new("RGBA", (5, 5), "black")
        frame.save(source / "movie.png", save_all=True, append_images=[Image.new("RGBA", (5, 5), "white")])
        (source / "huge.png").write_bytes(b"x" * (128 * 1024 + 1))
        truncated = (source / "icons/default.png").read_bytes()
        (source / "truncated.png").write_bytes(truncated[:-15])
        cases = (
            ("./../outside.png", 422, "plugin_icon_invalid"),
            ("https://example.invalid/icon.png", 422, "plugin_icon_invalid"),
            ("./icons/none.png", 404, "plugin_icon_missing"),
            ("./bad.png", 422, "plugin_icon_invalid"),
            ("./large.png", 422, "plugin_icon_invalid"),
            ("./movie.png", 422, "plugin_icon_invalid"),
            ("./huge.png", 422, "plugin_icon_invalid"),
            ("./truncated.png", 422, "plugin_icon_invalid"),
        )
        for declaration, status, code in cases:
            with self.subTest(declaration=declaration):
                section["icons"]["dark"] = declaration
                manifest.write_text(json.dumps(fields), encoding="utf8")
                record = self.install(source)
                self.assertEqual(self.icon(record, "dark", status=status)["code"], code)
                self.assertEqual(self.icon(record, "default")["resolvedTheme"], "default")

    def test_icon_read_revalidates_late_listener_and_retained_bytes(self):
        record = self.install(SAMPLES / "studio-v2")
        decode = plugin_contributions.decoded_icon

        def changed(raw):
            result = decode(raw)
            self.api("POST", record["id"], dict(expectedRevision=record["revision"], remove=True))
            return result

        with patch.object(plugin_contributions, "decoded_icon", side_effect=changed):
            self.assertEqual(self.icon(record, status=410)["code"], "plugin_missing")
        again = self.install(SAMPLES / "studio-v2")
        retained = Path(plugins.state()["plugins"][again["id"]]["root"]) / "icons/default.png"
        retained.write_bytes((SAMPLES / "namesake" / "icons/default.png").read_bytes())
        self.assertEqual(self.icon(again, status=409)["code"], "plugin_changed")

    def test_explicit_replace_and_retry_survive_missing_source(self):
        old = self.install(SAMPLES / "studio-v1")
        other = self.install(SAMPLES / "namesake")
        old = self.api("POST", old["id"], dict(expectedRevision=old["revision"], remoteEnabled=True,
                                                writeEnabled=True, confirm=True))
        directory = Path(shutil.copytree(SAMPLES / "studio-v2", self.root / "upgrade"))
        body = self.replacement(old, directory)
        url = old["id"] + "/replace"
        receipt = self.api("POST", url, body)
        self.assertEqual(receipt["previous"], {"id": old["id"], "fingerprint": old["fingerprint"],
                                              "revision": old["revision"] + 1})
        directory.rename(self.root / "moved-after-switch")
        self.assertEqual(self.api("POST", url, body), receipt)
        reader = ("import json,sys; from loop.plugin_runtime import route; "
                  "print(json.dumps(route([sys.argv[1],'replace'],'POST',{},json.load(sys.stdin))[1]))")
        restarted = subprocess.run([sys.executable, "-c", reader, old["id"]], input=json.dumps(body),
                                   cwd=SAMPLES.parents[1], env=dict(os.environ, HARNESS_DATA=str(self.root)),
                                   capture_output=True, text=True, check=True, timeout=15)
        self.assertEqual(json.loads(restarted.stdout), receipt)
        self.assertEqual(self.api("POST", url, dict(body, expectedRevision=1), status=409)["code"], "plugin_request_conflict")
        rows = {p["id"]: p for p in self.api("GET", "contributions")["plugins"]}
        self.assertEqual(set(rows), {other["id"], receipt["installed"]["id"]})
        self.assertEqual(rows[other["id"]]["revision"], other["revision"])
        new = rows[receipt["installed"]["id"]]
        self.assertEqual((new["fingerprint"], new["revision"]), (body["fingerprint"], 1))
        self.assertEqual([(r["kind"], r["localId"]) for r in new["contributions"]],
                         [("page", "overview"), ("page", "history")])

    def test_failed_preview_competing_replacements_and_same_name_neighbor(self):
        old = self.install(SAMPLES / "studio-v1")
        other = self.install(SAMPLES / "namesake")
        old = self.api("POST", old["id"], dict(expectedRevision=1, remoteEnabled=True, confirm=True))
        directory = Path(shutil.copytree(SAMPLES / "studio-v2", self.root / "mutable"))
        body = self.replacement(old, directory)
        url = old["id"] + "/replace"
        self.assertEqual(self.api("POST", url, dict(body, expectedRevision=1), status=409)["code"], "plugin_changed")
        (directory / "new.txt").write_text("changed since preview", encoding="utf8")
        self.assertEqual(self.api("POST", url, body, status=409)["code"], "plugin_changed")
        chosen = self.replacement(old, SAMPLES / "namesake")
        self.assertEqual(self.api("POST", url, chosen, status=409)["code"], "plugin_target_active")
        rows = {p["id"]: p for p in self.api("GET", "contributions")["plugins"]}
        self.assertEqual((set(rows), rows[old["id"]]["revision"]), ({old["id"], other["id"]}, 2))
        self.assertTrue(next(r for r in rows[old["id"]]["contributions"] if r["kind"] == "mcp")["enabled"])

        barrier = threading.Barrier(2)
        def compete(request):
            barrier.wait(timeout=5)
            try:
                return plugin_runtime.route([old["id"], "replace"], "POST", {}, request)
            except W.Problem as error:
                return error.status, {"code": error.code}
        left = self.replacement(old, SAMPLES / "studio-v2", "parallel-left")
        right = self.replacement(old, directory, "parallel-right")
        with ThreadPoolExecutor(2) as pool:
            responses = sorted(pool.map(compete, (left, right)), key=lambda entry: entry[0])
        self.assertEqual([entry[0] for entry in responses], [200, 409])
        self.assertEqual(responses[1][1]["code"], "plugin_changed")
        self.assertEqual({r["id"] for r in self.api("GET", "contributions")["plugins"]},
                         {other["id"], responses[0][1]["installed"]["id"]})

    def test_duplicate_local_declarations_rejected_before_publishing(self):
        source = Path(shutil.copytree(SAMPLES / "studio-v1", self.root / "duplicate"))
        (source/"mcp.json").write_text('{"mcpServers":{"notes":{},"notes":{}}}', encoding="utf8")
        result = self.api("POST", "preview", {"path": str(source)}, status=409)
        self.assertEqual(result["code"], "duplicate_key")
        self.assertEqual(self.api("GET", "contributions")["total"], 0)

    def test_publish_failure_and_concurrent_retry_keep_a_single_receipt(self):
        old = self.install(SAMPLES / "studio-v1")
        body = self.replacement(old, SAMPLES / "studio-v2")
        before = self.api("GET", "contributions")
        # Simulate a disk refusing the installed.json replacement, after the
        # new immutable directory is ready. Only public catalog state is asserted.
        with patch.object(plugins, "atomic_json", side_effect=OSError("test disk failure")):
            self.assertEqual(self.api("POST", old["id"]+"/replace", body, status=503)["code"], "plugin_store_unavailable")
        self.assertEqual(self.api("GET", "contributions"), before)
        barrier = threading.Barrier(2)
        def retry(_):
            barrier.wait(timeout=5)
            return plugin_runtime.route([old["id"], "replace"], "POST", {}, body)[1]
        with ThreadPoolExecutor(2) as pool:
            first, second = pool.map(retry, range(2))
        self.assertEqual(first, second)
        again = self.install(SAMPLES / "studio-v1")
        self.assertEqual(again["revision"], first["previous"]["revision"] + 1)
        self.assertEqual(self.api("POST", old["id"]+"/replace", body), first)
        self.assertEqual({item["id"] for item in self.api("GET", "contributions")["plugins"]},
                         {again["id"], first["installed"]["id"]})

    def test_four_samples_skill_draft_and_real_stdio_removal_reinstall(self):
        legacy = self.install(SAMPLES / "legacy-skill")
        studio = self.install(SAMPLES / "studio-v1")
        other = self.install(SAMPLES / "namesake")
        self.assertEqual((other["name"], other["version"]), (studio["name"], studio["version"]))
        self.assertNotEqual(other["id"], studio["id"])
        listing = self.api("GET", "contributions")["plugins"]
        legacy_row = next(row for row in listing if row["id"] == legacy["id"])
        self.assertEqual((legacy_row["version"], legacy_row["contributions"][0]["localId"]), ("unversioned", "outline"))
        self.api("POST", legacy["id"], dict(skillsEnabled=True, expectedRevision=1, confirm=True))
        draft = self.api("POST", legacy["id"] + "/skill-draft", {"name": "outline", "expectedRevision": 2})
        self.assertTrue(draft["text"])
        self.api("POST", legacy["id"], dict(remove=True, expectedRevision=2))
        self.assertEqual(self.api("POST", legacy["id"] + "/skill-draft",
                                 {"name": "outline", "expectedRevision": 2}, status=409)["code"], "plugin_changed")

        self.api("POST", studio["id"], dict(remoteEnabled=True, writeEnabled=True, confirm=True, expectedRevision=1))
        args = dict(plugin_id=studio["id"], server="notes")
        page = self.api("POST", studio["id"] + "/tools", {"server": "notes"})
        action = dict(args, tool="read_note", arguments={}, schema_hash=page["tools"][0]["schemaHash"])
        receipt = plugin_runtime.invoke(None, "call", action, human=True)
        self.assertEqual(receipt["result"]["content"][0]["text"], "Review note from studio v1.")
        self.api("POST", studio["id"], dict(remoteEnabled=False, expectedRevision=2))
        rows = next(p for p in self.api("GET", "contributions")["plugins"] if p["id"] == studio["id"])["contributions"]
        self.assertEqual([(x["localId"], x["status"]) for x in rows], [("overview", "disabled"), ("notes", "disabled")])
        self.assertFalse(rows[1]["writeEnabled"])
        with self.assertRaises(W.Problem) as denied:
            plugin_runtime.invoke(None, "call", action, human=True)
        self.assertEqual(denied.exception.code, "plugin_denied")
        self.api("POST", studio["id"], dict(remove=True, expectedRevision=3))
        self.assertEqual(self.api("POST", studio["id"], dict(expectedRevision=2, remove=True), status=410)["code"], "plugin_missing")
        again = self.install(SAMPLES / "studio-v1")
        self.assertEqual((again["id"], again["revision"], again["writeEnabled"]), (studio["id"], 5, False))
        self.assertEqual(self.api("POST", studio["id"], dict(expectedRevision=2, remove=True), status=409)["code"], "plugin_changed")
        self.api("POST", studio["id"], dict(expectedRevision=5, remoteEnabled=True, confirm=True))
        self.assertEqual(self.api("POST", studio["id"] + "/tools",
                                 dict(server="notes", catalog_hash=page["catalogHash"]), status=409)["code"], "plugin_catalog_changed")
        with self.assertRaises(W.Problem) as stale:
            plugin_runtime.invoke(None, "call", action, human=True)
        self.assertEqual(stale.exception.code, "plugin_schema_changed")

    def controlled_service(self):
        """The shipped notes service is read-only. Use a separate local MCP for delayed replies and a real write approval."""
        hold, entered, release = threading.Event(), threading.Event(), threading.Event()
        writes = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if "id" not in request:
                    self.send_response(202)
                    self.end_headers()
                    return
                if request["method"] == "initialize":
                    result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "test", "version": "1"}}
                elif request["method"] == "tools/list":
                    if hold.is_set():
                        entered.set()
                        release.wait(8)
                    result = {"tools": [{"name": "edit_note", "inputSchema": {"type": "object"}}]}
                else:
                    writes.append(request["params"])
                    result = {"content": [{"type": "text", "text": "saved"}]}
                raw = json.dumps({"jsonrpc":"2.0", "id":request["id"], "result":result}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.addCleanup(release.set)
        package = Path(shutil.copytree(SAMPLES / "studio-v1", self.root / "controlled"))
        url = f"http://127.0.0.1:{server.server_port}/mcp"
        (package / "mcp.json").write_text(json.dumps({"mcpServers": {"notes": {"url": url}}}), encoding="utf8")
        record = self.install(package)
        record = self.api("POST", record["id"], dict(expectedRevision=1, remoteEnabled=True, writeEnabled=True, confirm=True))
        return record, hold, entered, release, writes

    def test_late_tools_list_cannot_publish_after_uninstall_or_reinstall(self):
        record, hold, entered, release, writes = self.controlled_service()
        url = record["id"]
        hold.set()
        with ThreadPoolExecutor(2) as pool:
            request = pool.submit(W.handle, "POST", f"/api/v2/plugins/{url}/tools", {},
                                  {"server":"notes", "expectedRevision":record["revision"]}, self.sv)
            try:
                self.assertTrue(entered.wait(4))
                removed = pool.submit(self.api, "POST", url, dict(remove=True, expectedRevision=record["revision"]))
                self.assertTrue(removed.result(timeout=1.5)["removed"])
                again = self.install(self.root / "controlled")
                self.api("POST", url, dict(remoteEnabled=True, expectedRevision=again["revision"], confirm=True))
            finally:
                release.set()
            code, late = request.result(timeout=5)
        self.assertEqual((code, late["code"]), (409, "plugin_changed"))
        self.assertNotIn("tools", late)
        self.assertFalse(writes)
        self.assertEqual(self.api("POST", url + "/tools", dict(server="notes", expectedRevision=2), status=409)["code"], "plugin_changed")

    def test_pending_write_and_history_remain_bound_to_old_revision(self):
        record, _, _, _, writes = self.controlled_service()
        ident = self.call("POST")[1]["id"]
        projects.snapshot(ident)
        pid = record["id"]
        args = dict(plugin_id=pid, server="notes")
        page = plugin_runtime.invoke(ident, "inspect", args)
        action = dict(args, tool="edit_note", arguments={}, schema_hash=page["tools"][0]["schemaHash"])
        pending, resume = threading.Event(), threading.Event()

        def pause_caller():
            # A permission screen can outlive its worker. Keep the waiting caller
            # parked while the user replaces the package and answers its old prompt.
            if permissions.listing():
                pending.set()
                resume.wait(8)

        def call_write():
            context = set_call(dict(sessionId=ident, runId="a"*32, actionId="b"*32, tool="call_plugin_tool"))
            try:
                return plugin_runtime.invoke(ident, "call", action, validate_live=pause_caller)
            finally:
                reset_call(context)

        with ThreadPoolExecutor(1) as pool:
            request = pool.submit(call_write)
            try:
                self.assertTrue(pending.wait(5))
                row = permissions.listing()[0]
                self.assertEqual(row["state"], "pending")
                self.api("POST", pid + "/replace", self.replacement(record, SAMPLES / "studio-v2"))
                path = "/api/v2/permissions/" + row["id"]
                code, decision = W.handle("POST", path, {}, {"decision":"allow", "fingerprint":row["fingerprint"]}, self.sv)
                self.assertEqual((code, decision.get("code")), (409, "approval_expired"))
                code, history = W.handle("GET", path, {}, {}, self.sv)
                self.assertEqual((code, history["state"], history["action"]["pluginRevision"]), (200, "revoked", 2))
            finally:
                resume.set()
            with self.assertRaises(W.Problem):
                request.result(timeout=5)
        self.assertEqual(writes, [])

    def test_replacement_with_same_service_drops_credentials_and_writes(self):
        old, _, _, _, _ = self.controlled_service()
        connected = self.api("POST", old["id"]+"/connections/notes/token",
                             dict(expectedRevision=old["revision"], token="owned.test.credential"))
        old = dict(old, revision=connected["revision"])
        target = Path(shutil.copytree(self.root/"controlled", self.root/"next-with-notes"))
        manifest = target/"plugin.json"
        content = json.loads(manifest.read_text(encoding="utf8"))
        manifest.write_text(json.dumps(dict(content, version="2.0.0")), encoding="utf8")
        body = self.replacement(old, target)
        with patch.object(plugins, "atomic_json", side_effect=OSError("test disk failure")):
            self.api("POST", old["id"]+"/replace", body, status=503)
        before = self.api("GET", "contributions")["plugins"][0]
        mcp = next(r for r in before["contributions"] if r["kind"] == "mcp")
        self.assertEqual((mcp["authentication"], mcp["enabled"], mcp["writeEnabled"]), ("configured", True, True))
        self.api("POST", old["id"]+"/replace", body)
        after = self.api("GET", "contributions")["plugins"][0]
        new_mcp = next(r for r in after["contributions"] if r["kind"] == "mcp")
        self.assertEqual((new_mcp["authentication"], new_mcp["enabled"], new_mcp["writeEnabled"]), ("unknown", False, False))
        self.assertFalse(self.api("GET", "")["plugins"][0]["servers"][0]["connected"])

    def test_equal_count_switch_invalidates_page_two(self):
        studio = self.install(SAMPLES / "studio-v1")
        for index in range(20):
            package = self.root / f"fixture-{index}"
            package.mkdir()
            (package / "plugin.json").write_text(json.dumps({"name": package.name}), encoding="utf8")
            self.install(package)
        first = self.api("GET", "contributions")
        self.assertEqual((first["total"], len(first["plugins"])), (21, 20))
        old = {"cursor": [first["nextCursor"]]}
        last = self.api("GET", "contributions", query=old)
        self.assertEqual((last["snapshot"], len(last["plugins"]), last["nextCursor"]),
                         (first["snapshot"], 1, None))
        self.api("POST", studio["id"], {"expectedRevision": studio["revision"], "remoteEnabled": True, "confirm": True})
        expired = self.api("GET", "contributions", query=old, status=409)
        self.assertEqual(expired["code"], "plugin_catalog_stale")
        fresh = self.api("GET", "contributions")
        self.assertNotEqual(fresh["snapshot"], first["snapshot"])
        end = self.api("GET", "contributions", query={"cursor": [fresh["nextCursor"]]})
        self.assertEqual(fresh["snapshot"], end["snapshot"])
        rows = fresh["plugins"] + end["plugins"]
        self.assertEqual((fresh["total"], len({row["id"] for row in rows})), (21, 21))
        owner = next(row for row in rows if row["id"] == studio["id"])
        components = {(item["kind"], item["localId"]): item for item in owner["contributions"]}
        self.assertEqual(components["page", "overview"]["status"], "disabled")
        self.assertEqual(components["page", "overview"]["pageRevision"], 0)
        self.assertTrue(components["mcp", "notes"]["enabled"])
        self.assertEqual(self.api("GET", "contributions", query={"cursor": ["20"]}, status=400)["code"], "invalid_cursor")
        self.api("POST", owner["id"], dict(expectedRevision=owner["revision"], remove=True))
        stale_after_uninstall = self.api("GET", "contributions", query={"cursor":[fresh["nextCursor"]]}, status=409)
        self.assertEqual(stale_after_uninstall["code"], "plugin_catalog_stale")
