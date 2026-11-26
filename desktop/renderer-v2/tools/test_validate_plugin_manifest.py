"""Exercise the offline authoring check and the existing plugin host in a temporary store."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from validate_plugin_manifest import PREVIEW, ROOT, check_package
from loop import plugin_runtime, plugins, workbench


SAMPLES = ROOT / "examples" / "plugin-catalog"
CHECKER = Path(__file__).with_name("validate_plugin_manifest.py")
DIRS = ["legacy-skill", "namesake", "studio-v1", "studio-v2"]


def route(parts, body=None):
    method = "GET" if body is None else "POST"
    return plugin_runtime.route(parts, method, {}, body or {})[1]


def install(path):
    preview = route(["preview"], {"path": str(path)})
    return route(["install"], {"path": str(path), "fingerprint": preview["fingerprint"]})


class CatalogChecks(unittest.TestCase):
    def test_native_worker_is_recognized_offline(self):
        report = check_package(ROOT / "examples" / "worker-scout")
        self.assertIn({"kind": "worker", "localId": "investigate", "status": "recognized"}, report["contributions"])

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="Modot-catalog46-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = self.root / "data"
        self.env = patch.dict(os.environ, HARNESS_DATA=str(self.store), PYTHONDONTWRITEBYTECODE="1")
        self.env.start()
        self.addCleanup(self.env.stop)

    def cli(self, *paths):
        result = subprocess.run([sys.executable, "-B", str(CHECKER), "--json", *map(str, paths)],
                                cwd=self.root, capture_output=True, encoding="utf-8", env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        self.assertFalse(result.stderr, result.stderr)
        return result.returncode, json.loads(result.stdout)

    def test_all_samples_and_immutable_identity(self):
        directories = [SAMPLES / name for name in DIRS]
        copy = Path(shutil.copytree(SAMPLES / "studio-v1", self.root / "copy"))
        status, report = self.cli(*directories, copy)
        self.assertEqual(status, 0)
        self.assertTrue(report["ok"])
        old, other, mixed, upgrade, copied = report["packages"]
        self.assertEqual((old["format"], old["version"]), ("codex", "unversioned"))
        self.assertNotIn("packageKey", old)
        self.assertEqual(old["contributions"], [{"kind": "skill", "localId": "outline", "status": "recognized"}])
        self.assertEqual((other["name"], other["version"]), (mixed["name"], mixed["version"]))
        self.assertNotEqual(other["packageKey"], mixed["packageKey"])
        page = {"kind": "page", "localId": "overview", "status": "declared-only"}
        self.assertIn(page, other["contributions"])
        self.assertIn(page, mixed["contributions"])
        self.assertEqual({row["kind"] for row in mixed["contributions"]}, {"page", "mcp"})
        self.assertEqual(mixed["packageKey"], upgrade["packageKey"])
        self.assertEqual({row["localId"] for row in upgrade["contributions"]}, {"overview", "history"})
        self.assertEqual(len({p["fingerprint"] for p in (old, other, mixed, upgrade)}), 4)
        self.assertEqual(copied["fingerprint"], mixed["fingerprint"])
        # This would stop the process if executed while checking the package.
        (copy / "notes.py").write_text("raise SystemExit(87)\n", encoding="utf-8")
        changed = check_package(copy)
        self.assertTrue(changed["ok"])
        self.assertEqual(changed["packageKey"], mixed["packageKey"])
        self.assertNotEqual(changed["fingerprint"], mixed["fingerprint"])
        self.assertFalse(self.store.exists(), "Offline checks must not initialize an installation store")

    def test_a_claimed_package_key_cannot_prove_a_replacement(self):
        source = Path(shutil.copytree(SAMPLES / "namesake", self.root / "same-claim"))
        manifest = source / "plugin.json"
        data = json.loads(manifest.read_text("utf-8"))
        mixed = check_package(SAMPLES / "studio-v1")
        data["extensions"][PREVIEW]["packageKey"] = mixed["packageKey"]
        manifest.write_text(json.dumps(data), encoding="utf-8")
        other = check_package(source)
        self.assertEqual((other["packageKey"], other["name"], other["version"]),
                         (mixed["packageKey"], mixed["name"], mixed["version"]))
        self.assertNotEqual(other["fingerprint"], mixed["fingerprint"])
        self.assertNotEqual(install(source)["id"], install(SAMPLES / "studio-v1")["id"])
        self.assertEqual(route([])["total"], 2)

    def test_invalid_drafts_keep_independent_base_components(self):
        package = Path(shutil.copytree(SAMPLES / "studio-v1", self.root / "input"))
        path = package / "plugin.json"
        original = json.loads(path.read_text("utf-8"))
        for field, value, code in [
            ("version", 2, "catalog_version"), ("version", True, "catalog_version"),
            ("packageKey", "", "package_key"), ("pages", {"overview": True}, "page_ids"),
            ("pages", ["../notes.py"], "page_id"), ("pages", ["x", "x"], "duplicate_page")
        ]:
            with self.subTest(field=field, value=value):
                draft = {**original["extensions"][PREVIEW], field: value}
                path.write_text(json.dumps({**original, "extensions": {PREVIEW: draft}}), encoding="utf-8")
                result = check_package(package)
                self.assertFalse(result["ok"])
                self.assertEqual(result["diagnostics"][0]["code"], code)
                self.assertEqual([r["kind"] for r in result["contributions"]], ["mcp"])
        status, batch = self.cli(package, SAMPLES / "legacy-skill")
        self.assertEqual(status, 1)
        self.assertEqual([p["ok"] for p in batch["packages"]], [False, True])
        draft = {**original["extensions"][PREVIEW], "enabled": True, "installId": "untrusted"}
        path.write_text(json.dumps({**original, "extensions": {PREVIEW: draft}}), encoding="utf-8")
        result = check_package(package)
        self.assertEqual(result["diagnostics"][0]["code"], "unknown_fields")
        self.assertNotIn("installId", result)
        self.assertNotIn("enabled", result)

    def test_source_file_diagnostics(self):
        mixed = Path(shutil.copytree(SAMPLES / "studio-v1", self.root / "mixed"))
        (mixed / "mcp.json").write_text('{"mcpServers":{"notes":{},"notes":{"url":"https://example.invalid/"}}}', encoding="utf-8")
        result = check_package(mixed)
        self.assertFalse(result["ok"])
        self.assertEqual((result["diagnostics"][0]["field"], result["diagnostics"][0]["code"]), ("mcp.json", "duplicate_key"))
        old = Path(shutil.copytree(SAMPLES / "legacy-skill", self.root / "old"))
        (old / ".codex-plugin" / "plugin.json").write_text('{"name":"notes","mcpServers":"./missing.json"}', encoding="utf-8")
        status, report = self.cli(old, self.root / "absent", SAMPLES / "studio-v2")
        self.assertEqual(status, 1)
        self.assertEqual([p["ok"] for p in report["packages"]], [False, False, True])
        self.assertEqual(report["packages"][0]["diagnostics"][0]["code"], "missing_mcp")
        self.assertFalse(self.store.exists())

    def test_existing_formats_and_foreign_app_declarations(self):
        package = Path(shutil.copytree(SAMPLES / "legacy-skill", self.root / "old"))
        entry = package / ".codex-plugin" / "plugin.json"
        original = json.loads(entry.read_text("utf-8"))
        entry.write_text(json.dumps({**original, "apps": "./.app.json"}), encoding="utf-8")
        result = check_package(package)
        self.assertTrue(result["ok"])
        self.assertEqual([r["kind"] for r in result["contributions"]], ["skill"])
        self.assertIn("host_unsupported", {i["code"] for i in result["diagnostics"]})
        for name, kind in (("context-notes", "context-store"), ("memory-notes", "memory-provider")):
            result = check_package(ROOT / "examples" / name)
            self.assertTrue(result["ok"])
            self.assertEqual([r["kind"] for r in result["contributions"]], [kind])

    def test_real_install_revisions_removal_and_restore(self):
        records = [install(SAMPLES / name) for name in DIRS]
        old, other, mixed, upgrade = records
        self.assertEqual(len({p["id"] for p in records}), 4)
        self.assertEqual(route([])["total"], 4)
        for name, record in zip(DIRS, records):
            self.assertEqual(check_package(SAMPLES / name)["fingerprint"], record["fingerprint"])
            self.assertFalse(record["remoteEnabled"] or record["writeEnabled"] or record["skillsEnabled"])
        self.assertEqual(install(SAMPLES / "studio-v1")["revision"], mixed["revision"])
        self.assertNotIn("pages", mixed)
        self.assertEqual([(item["kind"], item["localId"], item["status"]) for item in mixed["contributions"]],
                         [("page", "overview", "disabled"), ("mcp", "notes", "disabled")])
        self.assertEqual([s["name"] for s in mixed["servers"]], ["notes"])
        self.assertEqual(upgrade["servers"], [])
        self.assertEqual(old["skills"][0]["name"], "outline")

        key, rev = mixed["id"], mixed["revision"]
        with self.assertRaises(workbench.Problem) as denied:
            route([key], {"expectedRevision": rev, "remoteEnabled": True})
        self.assertEqual(denied.exception.code, "confirmation")
        enabled = route([key], {"expectedRevision": rev, "remoteEnabled": True, "confirm": True})
        self.assertEqual([p["id"] for p in plugins.capture()], [key])
        self.assertFalse(enabled["writeEnabled"])
        with self.assertRaises(workbench.Problem) as stale:
            route([key], {"expectedRevision": rev, "remove": True})
        self.assertEqual((stale.exception.code, stale.exception.status), ("plugin_changed", 409))
        disabled = route([key], {"expectedRevision": enabled["revision"], "remoteEnabled": False})
        self.assertEqual(plugins.capture(), [])
        with self.assertRaises(workbench.Problem) as no_tools:
            route([key, "tools"], {"server": "notes"})
        self.assertEqual(no_tools.exception.code, "plugin_denied")
        self.assertEqual(disabled["fingerprint"], mixed["fingerprint"])
        removed = route([key], {"expectedRevision": disabled["revision"], "remove": True})
        self.assertTrue(removed["removed"])
        with self.assertRaises(workbench.Problem) as gone:
            route([key], {"expectedRevision": disabled["revision"], "remoteEnabled": True, "confirm": True})
        self.assertEqual((gone.exception.code, gone.exception.status), ("plugin_missing", 410))
        live = {p["id"]: p["revision"] for p in route([])["plugins"]}
        self.assertEqual(live, {p["id"]: p["revision"] for p in (old, other, upgrade)})
        self.assertTrue((SAMPLES / "studio-v1" / "notes.py").is_file())
        restored = install(SAMPLES / "studio-v1")
        self.assertEqual(restored["id"], key)
        self.assertGreater(restored["revision"], removed["revision"])
        self.assertFalse(restored["remoteEnabled"] or restored["writeEnabled"])
        self.assertFalse(restored["servers"][0]["connected"])

    def test_changed_bytes_rejected_before_install(self):
        source = Path(shutil.copytree(SAMPLES / "studio-v1", self.root / "source"))
        fingerprint = route(["preview"], {"path": str(source)})["fingerprint"]
        (source / "notes.py").write_text("# different content\n", encoding="utf-8")
        with self.assertRaises(workbench.Problem) as changed:
            route(["install"], {"path": str(source), "fingerprint": fingerprint})
        self.assertEqual((changed.exception.code, changed.exception.status), ("plugin_changed", 409))
        self.assertEqual(route([])["total"], 0)

    def test_revision_can_change_between_equal_total_pages(self):
        source = self.root / "filler"
        source.mkdir()
        for index in range(20):
            (source / "plugin.json").write_text(json.dumps({"name": f"filler-{index}"}), encoding="utf-8")
            install(source)
        mixed = install(SAMPLES / "studio-v1")
        first = route([])
        self.assertTrue(first["hasMore"])
        before = next(item for item in first["plugins"] if item["id"] == mixed["id"])
        changed = route([mixed["id"]], {"expectedRevision": before["revision"], "remoteEnabled": True, "confirm": True})
        second = plugin_runtime.route([], "GET", {"after": [str(first["next"])]}, {})[1]
        self.assertFalse(second["hasMore"])
        self.assertEqual(first["total"], second["total"])
        self.assertEqual(len({p["id"] for p in first["plugins"] + second["plugins"]}), 21)
        refreshed = next(p for p in route([])["plugins"] if p["id"] == mixed["id"])
        self.assertEqual(refreshed["revision"], changed["revision"])
        self.assertLess(before["revision"], refreshed["revision"])

    @unittest.skipUnless(os.name == "nt", "The repository's native stdio sandbox runs on Windows")
    def test_read_only_mcp_in_actual_host(self):
        package = install(SAMPLES / "studio-v1")
        pid = package["id"]
        enabled = route([pid], {"expectedRevision": package["revision"], "remoteEnabled": True, "confirm": True})
        runtime_dir = os.environ.get("HARNESS_SANDBOX_PYTHON_ROOT") or str(Path(sys.executable).parent)
        with patch.dict(os.environ, HARNESS_SANDBOX_PYTHON_ROOT=runtime_dir):
            page = route([pid, "tools"], {"server": "notes"})
            self.assertEqual((page["fingerprint"], page["revision"]), (package["fingerprint"], enabled["revision"]))
            self.assertEqual([t["spec"]["name"] for t in page["tools"]], ["read_note"])
            tool = page["tools"][0]
            self.assertTrue(tool["callable"])
            self.assertFalse(tool["requiresHuman"])
            response = plugin_runtime.invoke(None, "call", {
                "plugin_id": pid, "server": "notes", "tool": "read_note",
                "schema_hash": tool["schemaHash"], "arguments": {},
            }, human=True)
        self.assertTrue(response["ok"])
        self.assertEqual(response["result"]["content"][0]["text"], "Review note from studio v1.")


if __name__ == "__main__":
    unittest.main(verbosity=2)
