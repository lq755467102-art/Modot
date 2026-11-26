"""Historical output retrieval uses retained evidence, never original tools."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from tools.test_conversations import ConversationFixture
from tools import test_evidence_questions as Q
from loop import evidence, projects, workbench as W
from loop.tools import default_registry, set_call, reset_call


class HistoryTests(ConversationFixture, unittest.TestCase):
    source = Q.EvidenceQuestionTests.source

    def fixture(self, **event):
        thread, aid = self.source({"text": "原始输出😀" * 2000})
        directory = self.root / thread["id"]
        path = directory / "events.jsonl"
        row = json.loads(path.read_text())
        row.update(tool="inspect_delivery", args={})
        row.update(event)
        path.write_text(json.dumps(row) + "\n", encoding="utf8")
        requests = directory / "context-requests"
        requests.mkdir(exist_ok=True)
        refs = [dict(id=row['args']['resource_id'])] if row['args'].get('resource_id') else []
        (requests / "run-action.json").write_text(json.dumps(dict(
            runId="run", role="executor", resourceScope=dict(resources=refs))), encoding="utf8")
        root = W.workspace(projects.conversation(thread["id"])["workspaceId"])["path"]
        token = set_call(dict(sessionId=thread["id"], runId="current", actionId="reader"))
        self.addCleanup(reset_call, token)
        patcher = patch("loop.code_service.RuntimeView._resolve_sid", side_effect=self.sv._resolve_sid)
        patcher.start()
        self.addCleanup(patcher.stop)
        return thread, aid, root

    def test_complete_pages_require_same_revision_and_do_not_replay(self):
        thread, aid, root = self.fixture()
        reader = default_registry(Path(root)).get("read_action_evidence")
        self.assertTrue(reader.readonly)
        original = (self.root / thread["id"] / "events.jsonl").read_bytes()
        page = reader.fn(action_id=aid, run_id="run")
        self.assertTrue(page["historicalEvidence"])
        self.assertTrue(page["complete"])
        text = page["text"]
        while page["hasMore"]:
            page = reader.fn(action_id=aid, run_id="run", after=page["next"],
                             evidence_revision=page["evidenceRevision"])
            text += page["text"]
        self.assertEqual(json.loads(text)["text"], "原始输出😀" * 2000)
        self.assertEqual((self.root / thread["id"] / "events.jsonl").read_bytes(), original)
        for kwargs in (dict(run_id="other"), dict(run_id="run", after=2),
                       dict(run_id="run", evidence_revision="stale")):
            with self.assertRaises(W.Problem):
                reader.fn(action_id=aid, **kwargs)

    def test_other_owner_legacy_scope_and_recursive_evidence_are_denied(self):
        _, aid, root = self.fixture(tool="read_action_evidence")
        with self.assertRaises(W.Problem):
            evidence.tool_read(root, aid, "run")
        _, other = self.call("POST")
        token = set_call(dict(sessionId=other["id"], runId="other"))
        try:
            with self.assertRaises(W.Problem):
                evidence.tool_read(root, aid, "run")
        finally:
            reset_call(token)

    def test_public_dialogue_can_page_to_an_old_reference_without_other_scope(self):
        thread, _, root = self.fixture()
        from loop.context_archive import read_dialogue
        path = self.root / thread["id"] / "events.jsonl"
        requests = path.parent / "context-requests"
        requests.mkdir(exist_ok=True)
        (requests / "run-one.json").write_text(json.dumps(dict(runId="run", role="executor", resourceScope=dict(resources=[]))))
        rows = [dict(seq=1, act="say", role="executor", run_id="run", text="C = GitHub MCP"),
                dict(seq=2, act="say", role="executor", agent_id="child", text="C child")]
        rows.extend(dict(seq=i, act="agent_thought_chunk", text="C hidden") for i in range(3, 110))
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf8")
        page = read_dialogue(root, contains="C")
        self.assertEqual(page["turns"], [])
        self.assertTrue(page["hasMore"])
        older = read_dialogue(root, before=page["nextBefore"], contains="C")
        self.assertEqual([turn["text"] for turn in older["turns"]], ["C = GitHub MCP"])
        with self.assertRaises(W.Problem):
            read_dialogue(Path(root).parent)

    def test_revoked_resource_and_missing_output_are_explicit(self):
        _, aid, root = self.fixture(tool="read_project_resource", args={"resource_id": "revoked"})
        with self.assertRaises(W.Problem) as caught:
            evidence.tool_read(root, aid, "run")
        self.assertEqual(caught.exception.code, "resource_denied")

    def test_ordinary_folder_raw_action_rechecks_live_grants_on_every_page(self):
        from loop.work_folders import conversation_resource
        resource = conversation_resource()
        thread, aid, root = self.fixture(tool="run_project_check",args={"resource_id":resource["id"]})
        ident=thread["id"]
        W.change_registry(lambda data:data["threads"][ident].update(workResource=resource,resourceIds=[resource["id"]]))
        projects.snapshot(ident)
        first=evidence.tool_read(root,aid,"run")
        self.assertTrue(first["hasMore"])
        for change in (dict(permission="none"),dict(path=str(self.root))):
            W.change_registry(lambda data:data["threads"][ident].update(workResource=dict(resource,**change)))
            with self.assertRaises(W.Problem) as caught:
                evidence.tool_read(root,aid,"run",first["next"],first["evidenceRevision"])
            self.assertEqual(caught.exception.code,"resource_denied")

    def test_modified_retained_output_is_not_returned(self):
        thread, aid, root = self.fixture()
        (self.root / thread["id"] / "evidence-output" / (aid + ".json")).write_text("changed")
        with self.assertRaises(W.Problem) as caught:
            evidence.tool_read(root, aid, "run")
        self.assertEqual(caught.exception.code, "evidence_changed")

    def test_find_previous_action_without_its_ids_and_read_the_original(self):
        _, aid, root = self.fixture()
        reg = default_registry(Path(root))
        found = reg.get("search_conversation_history").fn(kind="action", contains="delivery")
        item, = found["items"]
        self.assertEqual((item["actionId"], item["runId"]), (aid, "run"))
        original = reg.get("read_action_evidence").fn(**item["read"]["arguments"])
        self.assertIn("原始输出", original["text"])

    def test_search_old_action_after_current_stream_and_resolve_quoted_id(self):
        from loop.conversation_history import search
        thread, aid, root = self.fixture(args={'scope_reason': 'compare public interfaces'})
        events = self.root / thread['id'] / 'events.jsonl'
        with events.open('a', encoding='utf8') as stream:
            for seq in range(2, 622):
                stream.write(json.dumps(dict(seq=seq, act='agent_thought_chunk', text='x',
                                            run_id='current', role='executor')) + '\n')
        for term in ('inspect_delivery', 'public interfaces', aid):
            with self.subTest(term=term):
                match, = search(root, contains=term)['items']
                self.assertEqual((match['actionId'], match['runId']), (aid, 'run'))
        with self.assertRaises(W.Problem) as mismatch:
            evidence.tool_read(root, aid, 'a-different-run')
        self.assertEqual(mismatch.exception.code, 'wrong_run')
        self.assertIn('run', str(mismatch.exception))

    def test_wrong_run_does_not_reveal_revoked_provenance_or_labels(self):
        from loop.conversation_history import search
        _, aid, root = self.fixture(tool='run_project_check',
            args={'resource_id': 'revoked', 'scope_reason': 'private old label'})
        self.assertFalse(search(root, contains='private old label')['items'])
        with self.assertRaises(W.Problem) as inaccessible:
            evidence.tool_read(root, aid, 'a-different-run')
        self.assertEqual(inaccessible.exception.code, 'resource_denied')

    def test_history_search_matches_current_access_before_and_after_removal(self):
        from uuid import uuid4

        rid = uuid4().hex
        thread, aid, root = self.fixture(tool="read_project_resource",
                                         args={"resource_id": rid, "path": "note.txt"})
        uploads = f"/{thread['id']}/attachments"
        status, _ = self.call("POST", uploads, {"importId": rid, "name": "note.txt", "data": "bm90ZQ=="})
        self.assertEqual(status, 200)
        projects.snapshot(thread["id"])
        search = default_registry(Path(root)).get("search_conversation_history").fn
        allowed, = search(kind="action")["items"]
        self.assertEqual(allowed["access"], "available")
        self.assertIn("read", allowed)

        self.assertEqual(self.call("POST", uploads, {"remove": rid})[0], 200)
        denied, = search(kind="action", contains="resource_denied")["items"]
        self.assertEqual(denied["actionId"], aid)
        self.assertEqual(denied["access"], "resource_denied")
        self.assertEqual(denied["historicalState"], allowed["state"])
        self.assertNotIn("state", denied)
        self.assertNotIn("read", denied)
        self.assertNotIn("path", denied)
        self.assertEqual(search(kind="action", access="resource_denied")["items"], [denied])
        self.assertEqual(search(kind="action", access="available")["items"], [])

    def test_search_matches_original_command_words_without_returning_script_body(self):
        from loop.conversation_history import search
        thread, aid, root = self.fixture(args={'script': 'import tomllib\nprint("payload-body")'})
        path = self.root / thread['id'] / 'events.jsonl'
        with path.open('a', encoding='utf8') as stream:
            for seq in range(2, 302):
                stream.write(json.dumps(dict(seq=seq, act='user', text='older unrelated public chat',
                                            run_id='unrelated')) + '\n')
        hit, = search(root, kind='action', contains='tomllib')['items']
        self.assertEqual((hit['actionId'], hit['matchedField']), (aid, 'script'))
        self.assertNotIn('payload-body', str(hit))

    def test_command_credentials_in_separate_arguments_are_hidden_from_lookup_and_read(self):
        from loop.conversation_history import search
        _, aid, root = self.fixture(args={'argv': ['example', '--token', 'private-fixture-value']})
        self.assertFalse(search(root, contains='private-fixture-value')['items'])
        visible = json.loads(evidence.tool_read(root, aid, 'run', view='input')['text'])
        self.assertEqual(visible['argv'][:2], ['example', '--token'])
        self.assertNotIn('private-fixture-value', visible['argv'])

    def test_dialogue_page_rechecks_scope_and_keeps_original_upper_bound(self):
        from loop import context_archive as A
        from loop.conversation_history import search
        from loop.work_folders import conversation_resource
        thread, _, root = self.fixture()
        directory = self.root / thread["id"]
        resource = conversation_resource()
        W.change_registry(lambda data: data["threads"][thread["id"]].update(
            workResource=resource, resourceIds=[resource["id"]]))
        projects.snapshot(thread["id"])
        snapshots = directory / "context-requests"
        snapshots.mkdir(exist_ok=True)
        (snapshots / "prior-one.json").write_text(json.dumps(dict(
            runId="prior", role="executor", resourceScope=dict(resources=[resource]))), encoding="utf8")
        rows = [dict(act="say", text="scope-bound needle " + "x" * 4010 + " END")]
        rows.extend(dict(act="user", role="user", text=f"question {n}") for n in range(15))
        rows.extend([dict(act="say", agent_id="child", text="hidden child"),
                     dict(act="agent_thought_chunk", text="hidden reasoning")])
        for n, row in enumerate(rows, 1):
            row.update(seq=n, run_id="prior")
        events = directory / "events.jsonl"
        events.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf8")
        first = search(root, kind="dialogue")
        with events.open("a", encoding="utf8") as file:
            file.write(json.dumps(dict(act="user", seq=19, text="newly appended", run_id="later")) + "\n")
        with self.assertRaises(W.Problem) as changed_cursor:
            search(root, kind="dialogue", cursor=dict(first["nextCursor"], upper=events.stat().st_size))
        self.assertEqual(changed_cursor.exception.code, "invalid_cursor")
        rest = search(root, kind="dialogue", cursor=first["nextCursor"])
        combined = first["items"] + rest["items"]
        self.assertEqual(len(combined), 16)
        self.assertEqual(len({item["seq"] for item in combined}), 16)
        self.assertNotIn("newly appended", str(combined))
        self.assertNotIn("hidden", str(combined))
        ref = next(item["read"]["arguments"] for item in combined if item["seq"] == 1)
        page = A.read_dialogue(root, **ref)
        self.assertEqual(page["text"] + A.read_dialogue(root, offset=page["nextOffset"], **ref)["text"], rows[0]["text"])
        with self.assertRaises(W.Problem) as stale:
            A.read_dialogue(root, position=ref["position"], message_version="stale")
        self.assertEqual(stale.exception.code, "evidence_changed")
        W.change_registry(lambda data: data["threads"][thread["id"]].update(resourceIds=[]))
        self.assertFalse(search(root, kind="dialogue", contains="scope-bound needle")["items"])
        self.assertNotIn("scope-bound needle", str(search(root, kind="dialogue", cursor=first["nextCursor"])))
        with self.assertRaises(W.Problem) as denied:
            A.read_dialogue(root, offset=page["nextOffset"], **ref)
        self.assertEqual(denied.exception.code, "resource_denied")

    def test_missing_original_is_distinguished_from_retained_parameters(self):
        from loop.conversation_history import search
        thread, aid, root = self.fixture()
        (self.root / thread["id"] / "evidence-output" / (aid + ".json")).unlink()
        item, = search(root, kind="action")["items"]
        self.assertEqual(item["access"], "output_missing")
        self.assertEqual(item["read"]["arguments"]["view"], "input")
        with self.assertRaises(W.Problem) as missing:
            evidence.tool_read(root, aid, "run")
        self.assertEqual(missing.exception.code, "evidence_missing")

    def test_unscoped_metadata_cannot_quote_a_revoked_run(self):
        thread, aid, root = self.fixture()
        path = self.root / thread["id"] / "context-requests" / "run-action.json"
        path.write_text(json.dumps(dict(runId="run", role="executor",
                            resourceScope=dict(resources=[dict(id="previous")]))), encoding="utf8")
        with self.assertRaises(W.Problem) as revoked:
            evidence.tool_read(root, aid, "run")
        self.assertEqual(revoked.exception.code, "resource_denied")
        path.unlink()
        with self.assertRaises(W.Problem) as unknown:
            evidence.tool_read(root, aid, "run")
        self.assertEqual(unknown.exception.code, "scope_unknown")

    def test_rebinding_the_same_resource_id_does_not_restore_old_script_access(self):
        from loop.work_folders import conversation_resource
        resource = conversation_resource()
        thread, aid, root = self.fixture(tool='run_project_check', args=dict(resource_id=resource['id']))
        ident = thread['id']
        W.change_registry(lambda data: data['threads'][ident].update(workResource=resource, resourceIds=[resource['id']]))
        origin = projects.snapshot(ident)
        path = self.root / ident / "context-requests" / "run-action.json"
        path.write_text(json.dumps(dict(runId='run', role='executor', resourceScope=origin)), encoding='utf8')
        self.assertTrue(evidence.tool_read(root, aid, 'run', view='input')['ok'])
        W.change_registry(lambda data: data['threads'][ident].update(workResource=dict(resource, path=str(self.root))))
        projects.snapshot(ident)
        with self.assertRaises(W.Problem) as refused:
            evidence.tool_read(root, aid, 'run', view='input')
        self.assertEqual(refused.exception.code, 'resource_denied')


if __name__ == "__main__":
    unittest.main()
