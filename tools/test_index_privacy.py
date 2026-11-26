import os
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from loop import index


class IndexPrivacy(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.root = base / "project"
        self.root.mkdir()
        env = patch.dict(os.environ, {
            "HARNESS_DATA": str(base / "profile"),
            "HARNESS_INDEX_DIR": str(base / "indexes"),
            "GIT_CONFIG_GLOBAL": str(base / "empty"),
            "GIT_CONFIG_NOSYSTEM": "1",
        })
        env.start()
        self.addCleanup(env.stop)

    def put(self, name, content="ordinary test fixture"):
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf8")

    def indexed(self):
        result = index.build(self.root)
        self.assertTrue(result["ok"], result)
        with closing(sqlite3.connect(index.db_path(self.root))) as db:
            chunks = {row[0] for row in db.execute("SELECT DISTINCT path FROM chunks")}
            files = {row[0] for row in db.execute("SELECT path FROM files")}
        self.assertEqual(files, chunks)
        return files

    def test_rebuild_removes_newly_ignored_tracked_and_untracked_files(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        for name in ("main.py", "draft.md", "tracked.md"):
            self.put(name)
        subprocess.run(["git", "-C", str(self.root), "add", "tracked.md"], check=True)
        self.assertIn("draft.md", self.indexed())
        self.put(".gitignore", "*.md\n")
        self.assertEqual(self.indexed(), {".gitignore", "main.py"})

    def test_plain_project_nested_rules_and_modot_data(self):
        self.put("docs/.gitignore", "*.txt\n!public.txt\n")
        self.put("docs/public.txt")
        self.put("docs/private.txt")
        self.put("src/main.py")
        for name in ("api-token", ".env.local", ".harness/agent-memory/main/MEMORY.md",
                     "memory/inbox/item.md", "memory/tree/rule.md", "runs/task/events.jsonl"):
            self.put(name)
        self.assertEqual(self.indexed(), {"docs/.gitignore", "docs/public.txt", "src/main.py"})
        self.assertFalse((self.root / ".git").exists())

    def test_git_failure_does_not_fall_back_to_reading_ignored_content(self):
        self.put(".gitignore", "draft.md\n")
        self.put("draft.md")
        with patch.dict(os.environ, {"HARNESS_GIT": str(self.root / "missing-git")}):
            self.assertFalse(index.build(self.root)["ok"])
        self.assertFalse(index.db_path(self.root).exists())
