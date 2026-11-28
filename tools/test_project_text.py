"""Information contracts exercised through scoped code tools and request assembly."""
import unittest
from unittest.mock import patch

from loop import code_service as C, workbench as W
from tools.test_conversations import ConversationFixture
from tools import test_agent_code as code_fixture
from loop.tools import set_call, reset_call


class SearchInformationTests(ConversationFixture, unittest.TestCase):
    setup_code = code_fixture.AgentCodeTests.setup_code
    grant = code_fixture.AgentCodeTests.grant

    def setUp(self):
        super().setUp()
        self.code = self.setup_code()
        token = set_call(dict(sessionId=self.t['id'], runId='a'*32, actionId='b'*32, role='executor'))
        self.addCleanup(reset_call, token)

    def search(self, **args):
        return C.invoke(self.execution_root, 'search', self.r['id'], **args)

    def test_large_project_text_search_returns_the_actual_tail(self):
        path = self.code / 'Large.prefab'
        with path.open('wb') as stream:
            stream.write(b'# padding\n' * 240000)
            stream.write('material: 尾端引用\n'.encode())
        page = self.search(query='尾端引用', relative_path=path.name)
        self.assertTrue(page['ok'], page)
        self.assertFalse(page['truncated'], page)
        self.assertEqual([(m['line'], m['text']) for m in page['matches']],
                         [(240001, 'material: 尾端引用')])

    def test_large_read_continues_to_requested_line_and_never_returns_scanned_padding(self):
        from loop import project_text, projects
        name = "crossing.prefab"
        unit = "padding µ\n".encode()
        target = "正文跨块🙂\r\n"
        prefix = unit * (W.LIMIT // len(unit) + 1)
        # Put the UTF-8 character and its newline on different I/O blocks.
        extra = (W.TEXT_PAGE_BYTES - (len(prefix) + len("正文".encode()) + 1) % W.TEXT_PAGE_BYTES) % W.TEXT_PAGE_BYTES
        data = prefix + b"-" * (extra - 1) + b"\n" + target.encode() + b"untouched\n"
        (self.code / name).write_bytes(data)
        number = prefix.count(b"\n") + 2
        with patch.object(project_text, "SCAN_BYTES", 512 * 1024):
            page = projects.tool_read(self.execution_root, self.r["id"], path=name, line=number, line_count=1)
            receipt = []
            for _ in range(10):
                self.assertTrue(page["ok"], page)
                receipt.append(page)
                if not page["truncated"]:
                    break
                page = projects.tool_read(self.execution_root, self.r["id"], path=name, cursor=page["nextCursor"])
        self.assertGreater(len(receipt), 1)
        self.assertTrue(all(p["text"] == "" and p["readKind"] == "scan_progress" for p in receipt[:-1]), receipt)
        self.assertEqual((page["startLine"], page["endLine"], page["text"]), (number, number, target))
        self.assertTrue(page["hasMore"])
        self.assertIsNone(page["nextOffset"])
        after = projects.tool_read(self.execution_root, self.r["id"], path=name, cursor=page["nextCursor"])
        self.assertEqual(after["text"], "untouched\n")

    def test_large_search_resumes_inside_a_line_and_finds_split_unicode_once(self):
        from loop import project_text
        name = "wide.prefab"
        block = W.TEXT_PAGE_BYTES
        needle = "甲乙丙"
        lead = b"a" * (block - 4)
        content = (lead + needle.encode() + b"a"*block + needle.encode() +
                   b"\r\nbefore\n" + needle.encode() + b"\nafter\n" + b"a" * W.LIMIT)
        (self.code / name).write_bytes(content)
        params = dict(query=needle, relative_path=name, limit=1, context_lines=1)
        records = []
        with patch.object(project_text, "SCAN_BYTES", block):
            page = self.search(**params)
            for _ in range(50):
                self.assertTrue(page["ok"], page)
                records.extend(page["matches"])
                if not page["truncated"]:
                    break
                page = self.search(**params, cursor=page["nextCursor"])
        self.assertFalse(page["truncated"])
        self.assertEqual([m["line"] for m in records], [1, 3])
        self.assertIn(needle, records[0]["text"])
        self.assertEqual(records[1]["context"]["text"], "before\n甲乙丙\nafter")

    def test_unsearched_binary_is_explicit_and_whole_file_edit_stays_bounded(self):
        name = "binary.asset"
        (self.code / name).write_bytes(b"\0" + b"\xff" * W.LIMIT)
        page = self.search(query="absent", relative_path=name)
        self.assertEqual(page["pageCoverage"], "has_skips")
        self.assertEqual(page["skippedByReason"], {"binary": 1})
        self.assertFalse(page["matches"])
        inspect = C.invoke(self.execution_root, 'inspect', self.r['id'], relative_path=name, line=1, line_count=1)
        self.assertFalse(inspect["ok"])
        with self.assertRaises(W.Problem) as error:
            W.editable_bytes({"path": str(self.code)}, name)
        self.assertEqual(error.exception.code, "large_file")

    def test_stream_positions_check_version_current_authority_and_tool_scope(self):
        from loop import project_text, projects
        name = "tail.txt"
        payload = b"a\n" * (W.LIMIT // 2 + 1) + b"marker\n"
        (self.code / name).write_bytes(payload)
        params = dict(query="marker", relative_path=name)
        def read(**kw):
            return C.invoke(self.execution_root, 'inspect', self.r['id'], relative_path=name, **kw)
        with patch.object(project_text, "SCAN_BYTES", W.TEXT_PAGE_BYTES):
            search_cursor = self.search(**params)["nextCursor"]
            read_cursor = read(line=W.LIMIT // 2 + 2, line_count=1)["nextCursor"]
        self.assertEqual(read(cursor=search_cursor)["code"], "invalid_cursor")
        self.assertEqual(self.search(query="other", relative_path=name, cursor=search_cursor)["code"], "search_cursor_stale")
        (self.code / name).write_bytes(b"b" + payload[1:])
        self.assertEqual(self.search(**params, cursor=search_cursor)["code"], "search_cursor_stale")
        self.assertEqual(read(cursor=read_cursor)["code"], "revision_conflict")
        projects.set_scope(self.t['id'], {'projectId':self.p['id'], 'resourceIds':[]}, self.sv)
        for denied in (read(cursor=read_cursor), self.search(**params,cursor=search_cursor)):
            self.assertEqual(denied["code"], "resource_denied")
            self.assertFalse(set(denied) & {"matches", "text", "nextCursor"})

    def test_read_only_line_fragments_join_exactly_and_an_offset_still_counts_characters(self):
        from loop import projects
        name = "long-line.txt"
        first = "材🙂" * 1200 + "\r\n"
        (self.code / name).write_bytes(b"\xef\xbb\xbf" + (first + "next\n").encode() + b"-"*W.LIMIT)
        page = C.invoke(self.execution_root, 'inspect', self.r['id'], relative_path=name, line=1, line_count=1)
        self.assertEqual((page["endLine"], page["truncationReason"], len(page["text"])), (1, "line_fragment", 2000))
        tail = projects.tool_read(self.execution_root, self.r['id'], path=name, cursor=page["nextCursor"])
        self.assertEqual((tail["startLine"], tail["endLine"], tail["truncated"]), (1, 1, False))
        self.assertEqual(page["text"]+tail["text"], first)
        by_offset = projects.tool_read(self.execution_root, self.r['id'], path=name, offset=len(first)-4)
        self.assertTrue(by_offset["text"].startswith(first[-4:]+"next\n"))
        self.assertLessEqual(len(by_offset["text"].encode()), 50*1024)

    def test_later_search_pages_preserve_skipped_coverage(self):
        from loop import project_text
        folder = self.code / "mixture"
        folder.mkdir()
        (folder / "a.bin").write_bytes(b"\x00" * 11)
        (folder / "b.txt").write_bytes(b"-"*W.LIMIT + b"\nmark\n")
        params = dict(query="mark", relative_path="mixture")
        with patch.object(project_text, "SCAN_BYTES", 1024*1024):
            page = self.search(**params)
            self.assertTrue(page["truncated"])
            self.assertEqual(page["skippedFileCount"], 1)
            while page["truncated"]:
                page = self.search(**params, cursor=page["nextCursor"])
        self.assertEqual(page["matches"][0]["line"], 2)
        self.assertEqual((page["pageCoverage"], page["skippedFileCount"]), ("has_skips", 1))

    def test_permission_withdrawn_during_the_scan_suppresses_the_new_page(self):
        from loop import project_text, projects
        name = "long.txt"
        (self.code / name).write_bytes(b"private\n" + b"-"*W.LIMIT)
        original = project_text.read
        def withdraw(*args):
            result = original(*args)
            projects.set_scope(self.t['id'], {'projectId':self.p['id'], 'resourceIds':[]}, self.sv)
            return result
        with patch.object(project_text, "read", side_effect=withdraw):
            denied = projects.tool_read(self.execution_root, self.r['id'], path=name)
        self.assertEqual(denied["code"], "resource_denied", denied)
        self.assertNotIn("text", denied)
