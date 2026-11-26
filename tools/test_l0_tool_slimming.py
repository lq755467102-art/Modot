"""Concrete tool contracts from the Flash trace; real service, no model calls."""
import json
from base64 import b64encode
import unittest
from pathlib import Path
from unittest.mock import patch

from loop import code_service as C, delivery_review as D, projects, workbench as W
from loop import executor_core as EC
from loop import loop as L
from loop.provider import MockProvider, OpenAICompatProvider
from loop.tools import default_registry, set_call, reset_call
from tools.test_conversations import ConversationFixture
from tools import test_agent_code as fixtures


class ToolSlimmingTests(ConversationFixture, unittest.TestCase):
    setup_code = fixtures.AgentCodeTests.setup_code
    grant = fixtures.AgentCodeTests.grant

    def setUp(self):
        super().setUp()
        self.code = self.setup_code()
        W.safe_git(self.ws,['switch','-c','agent/tool-contracts'])
        token=set_call(dict(sessionId=self.t['id'],runId='a'*32,actionId='b'*32,role='executor'))
        self.addCleanup(reset_call,token)

    def test_line_read_returns_requested_function_without_character_repagination(self):
        text = ''.join(f'line_{i:03} = ' + 'x'*50 + '\r\n' for i in range(1,181))
        (self.code/'large.py').write_bytes(text.encode())
        out = C.invoke(self.execution_root,'inspect',self.r['id'],relative_path='large.py',line=1,line_count=180)
        self.assertTrue(out['ok'],out)
        self.assertEqual(out['text'],text)
        self.assertEqual((out['startLine'],out['endLine']),(1,180))
        self.assertFalse(out['truncated'])
        self.assertIsNone(out['nextOffset'])

    def test_full_bounded_code_page_reaches_next_model_input(self):
        text=''.join(f"line_{i:03} = '" + 'x'*50 + "'\r\n" for i in range(180))
        (self.code/'large.py').write_bytes(text.encode('utf8'))
        provider=MockProvider([dict(tool='inspect_code',args=dict(resource_id=self.r['id'],
            relative_path='large.py',line=1,line_count=180)),dict(text='Read-only range checked')])
        L.run('Read the selected code range',provider,default_registry(self.execution_root),
              lambda _:dict(ok=True),session=self.session,max_turns=2,executor_only=True)
        messages=self.session.load()['msgs']
        page=next(json.loads(m['content']) for m in messages if m['role']=='tool')
        self.assertEqual(page['text'],text)
        self.assertNotIn('out_ref',page)

    def test_native_l0_reads_the_original_project_image(self):
        original = (Path(__file__).resolve().parents[1] / "docs/images/workroom-app-card.png").read_bytes()
        (self.code / "screenshot.png").write_bytes(original)
        self.session.task_input = dict(id="image-task", revision=1, kind="new")
        registry = default_registry(self.execution_root)
        core = EC.ExecutorCore(self.session, self.execution_root)
        core.install(registry)
        self.assertIn("read_image", {tool["name"] for tool in registry.schemas()})

        provider = MockProvider([
            dict(tool="read_image", args=dict(path="screenshot.png")),
            dict(text="Image received"),
        ])
        result = L.run("What is written in screenshot.png?", provider, registry, lambda _: dict(ok=True),
                       session=self.session, max_turns=2, executor_only=True, execution_core=core)
        self.assertEqual(result["status"], "success", result["why"])
        images = [
            part["image_url"]["url"]
            for message in provider.seen[1]["texts"] if isinstance(message, list)
            for part in message if part["type"] == "image_url"
        ]
        self.assertEqual(images, ["data:image/png;base64," + b64encode(original).decode("ascii")])

    def test_native_l0_image_read_requires_current_project_authorization(self):
        original = (Path(__file__).resolve().parents[1] / "docs/images/workroom-app-card.png").read_bytes()
        (self.code / "inside.png").write_bytes(original)
        outside = self.root / "outside.png"
        outside.write_bytes(original)
        registry = default_registry(self.execution_root)
        EC.ExecutorCore(self.session, self.execution_root).install(registry)
        read_image = registry.get("read_image")

        for path in ("../outside.png", str(outside)):
            with self.subTest(path=path):
                result = read_image({"path": path})
                self.assertEqual(result["code"], "invalid_path")
                self.assertNotIn("image", result)

        self.grant(permission="none")
        result = read_image({"path": "inside.png", "resource_id": self.r["id"]})
        self.assertEqual(result["code"], "resource_denied")
        self.assertNotIn("image", result)

    def test_l0_can_project_gcode_without_any_worker_and_respects_project_scope(self):
        from hashlib import sha256

        source = (b"G90\nM83\nM486 S0\nM486 AFront panel\nG1 X0 Y0 Z0.2\n"
                  b"G1X5Y5E1\nG92 E0\nG1X10Y10E1\n"
                  b"M486 S1\nM486 AFixture\nG0 X70 Y0\nG1X75Y10E1\n")
        path = self.code / "panel.gcode"
        path.write_bytes(source)
        registry = default_registry(self.execution_root)
        with patch("loop.plugins.grants", return_value=[]):
            EC.ExecutorCore(self.session, self.execution_root).install(registry)
        self.assertNotIn("inspect_worker_plugins", {tool["name"] for tool in registry.schemas()})
        read = registry.get("read_image")
        full = read({"path": "panel.gcode", "view": {}})
        selected = read({"path": "panel.gcode", "view": {"object_id": 0, "align": "principal"}})
        self.assertTrue(selected["ok"], selected)
        self.assertEqual(selected["sourceSha256"], sha256(source).hexdigest())
        self.assertEqual(selected["view"]["selectedMoves"], 2)
        self.assertEqual(selected["view"]["principalAxisDegrees"], 45)
        self.assertEqual([o["name"] for o in full["view"]["availableObjects"]], ["Front panel", "Fixture"])
        self.assertNotEqual(full["image"]["hash"], selected["image"]["hash"])
        self.assertEqual(path.read_bytes(), source)
        self.assertEqual([p.name for p in path.parent.glob("*.png")], [])
        self.grant(permission="none")
        denied = read({"path": "panel.gcode", "view": {}, "resource_id": self.r["id"]})
        self.assertEqual(denied["code"], "resource_denied")

    def test_configured_vision_receives_only_authorized_image_and_returns_transcription(self):
        import os

        from loop import models, runtime

        self.session.run_id = runtime.register(self.session.dir, os.getpid())["run_id"]

        original = (Path(__file__).resolve().parents[1] / "docs/images/workroom-app-card.png").read_bytes()
        (self.code / "transcribe.png").write_bytes(original)
        url = "data:image/png;base64," + b64encode(original).decode("ascii")
        self.session.task_input = dict(id="image-task", revision=1, kind="new")
        requests = []

        def wire(_provider, request, _stop):
            requests.append(json.loads(request.data))
            yield b'data: {"choices":[{"delta":{"content":"i.I 42!"},"finish_reason":"stop"}]}\n'
            yield b'data: {"choices":[],"usage":{"prompt_tokens":321,"completion_tokens":7}}\n'
            yield b'data: [DONE]\n'

        with patch.dict("os.environ", {"HARNESS_MODELS": str(self.root / "models.json")}):
            profile = models.user_action(dict(act="save", name="Vision fixture", model="vision-fixture",
                                             api_key="vision-fixture-credential", base_url="https://fixture.example/v1"))
            self.assertTrue(profile["ok"])
            models.user_action(dict(act="select", role="main", id=""))
            registry = default_registry(self.execution_root)
            EC.ExecutorCore(self.session, self.execution_root).install(registry)
            self.assertNotIn("transcribe_image", {tool["name"] for tool in registry.schemas()})

            self.assertTrue(models.user_action(dict(act="select", role="vision", id=profile["id"]))["ok"])
            self.assertEqual(models.public()["main"], "")
            self.assertEqual(models.public()["vision"], profile["id"])
            registry = default_registry(self.execution_root)
            core = EC.ExecutorCore(self.session, self.execution_root)
            core.install(registry)
            self.assertIn("transcribe_image", {tool["name"] for tool in registry.schemas()})
            primary = MockProvider([
                dict(tool="transcribe_image", args=dict(path="transcribe.png")),
                dict(text="Used the transcription"),
            ])
            with patch.object(OpenAICompatProvider, "_iter_lines", wire):
                result = L.run("Private parent prompt", primary, registry, lambda _: dict(ok=True),
                               session=self.session, max_turns=2, executor_only=True, execution_core=core)
                self.assertEqual(result["status"], "success", result["why"])
                self.grant(permission="none")
                denied = registry.get("transcribe_image")({"path": "transcribe.png", "resource_id": self.r["id"]})
                self.assertEqual(denied["code"], "resource_denied")
                self.assertTrue(models.user_action(dict(act="del", id=profile["id"]))["ok"])
                self.assertEqual(models.public()["vision"], "")

        messages = self.session.load()["msgs"]
        output = next(json.loads(message["content"]) for message in messages if message["role"] == "tool")
        self.assertEqual(len(requests), 1, output)
        request = requests[0]
        self.assertEqual(request["model"], "vision-fixture")
        self.assertNotIn("tools", request)
        self.assertEqual(len(request["messages"]), 1)
        content = request["messages"][0]["content"]
        self.assertEqual(content[1], {"type": "image_url", "image_url": {"url": url}})
        self.assertNotIn("Private parent prompt", json.dumps(request))
        self.assertEqual(output["text"], "i.I 42!")
        self.assertNotIn("data:image", json.dumps(output))

    def test_oversized_line_range_is_explicit_and_continuation_is_lossless(self):
        text = ''.join('x'*220+'\n' for _ in range(400))
        (self.code/'large.py').write_bytes(text.encode('utf8'))
        first = C.invoke(self.execution_root,'inspect',self.r['id'],relative_path='large.py',line=1,line_count=400)
        self.assertTrue(first['truncated'])
        self.assertLessEqual(len(first['text'].encode('utf8')),50*1024)
        result=first['text']; cursor=first['nextOffset']
        while cursor is not None:
            page=C.invoke(self.execution_root,'inspect',self.r['id'],relative_path='large.py',offset=cursor)
            result+=page['text'];cursor=page['nextOffset']
        self.assertEqual(result,text)

    def test_default_read_window_is_not_spilled_a_second_time(self):
        text=''.join(f"value_{i} = '"+'source '*8+"'\n" for i in range(300))
        self.assertGreater(len(text),12000)
        (self.code/'window.py').write_bytes(text.encode('utf8'))
        provider=MockProvider([dict(tool='inspect_code',args=dict(resource_id=self.r['id'],
            relative_path='window.py')),dict(text='Read the default window')])
        L.run('Read this source',provider,default_registry(self.execution_root),lambda _:dict(ok=True),
              session=self.session,max_turns=2,executor_only=True)
        page=next(json.loads(m['content']) for m in self.session.load()['msgs'] if m['role']=='tool')
        self.assertEqual(page['text'],text)
        self.assertNotIn('out_ref',page)

    def test_code_resource_reader_and_inspect_share_bytes_revision_and_locations(self):
        text = '# header\r\n' + 'line = 1\r\n'*500
        (self.code/'large.py').write_bytes(text.encode())
        generic=projects.tool_read(self.execution_root,self.r['id'],'large.py')
        focused=C.invoke(self.execution_root,'inspect',self.r['id'],relative_path='large.py')
        for key in ('text','revision','startLine','endLine','nextOffset','truncated'):
            self.assertEqual(generic[key],focused[key],key)
        for offset in (-1, True, '0'):
            with self.assertRaises(W.Problem):
                projects.tool_read(self.execution_root,self.r['id'],'large.py',offset=offset)

    def test_original_search_limit_and_unknown_field_errors_identify_field(self):
        for query in ('nonexisting','genindex','env.tocs'):
            out=C.invoke(self.execution_root,'search',self.r['id'],query=query,limit=40)
            self.assertEqual(out['code'],'invalid_arguments')
            self.assertIn('limit',out['error']);self.assertIn('30',out['error'])
        out=C.invoke(self.execution_root,'search',self.r['id'],query='test',resource_path='tests')
        self.assertIn('resource_path',out['error']);self.assertIn('relative_path',out['error'])

    def test_line_offset_conflict_names_both_fields(self):
        out=C.invoke(self.execution_root,'inspect',self.r['id'],relative_path='calc.py',line=1,offset=0)
        self.assertIn('line',out['error']);self.assertIn('offset',out['error'])

    def test_stale_project_version_is_not_misdiagnosed_as_a_file_hash(self):
        previous=W.current_revision(self.ws)
        (self.code/'user-note.txt').write_text('USER_CHANGE',encoding='utf8')
        out=C.invoke(self.execution_root,'check',self.r['id'],argv=['python','check.py'],expected_revision=previous)
        self.assertEqual(out['code'],'revision_conflict')
        self.assertEqual(out['expectedProjectRevision'],previous)
        self.assertNotEqual(out['projectRevision'],previous)
        self.assertNotIn('不能用单文件',out['error'])
        self.assertEqual((self.code/'user-note.txt').read_text(),'USER_CHANGE')

    def test_missing_target_revision_is_not_a_concurrent_edit(self):
        selected=D.current_target(self.t['id'],dict(kind='code',id=self.r['id']))
        with self.assertRaises(W.Problem) as caught:
            D.bind_target(self.t['id'],dict(kind='code',id=self.r['id']))
        self.assertEqual(caught.exception.code,'target_revision_required')
        self.assertIn('target.revision',caught.exception.message)
        self.assertEqual(D.bind_target(self.t['id'],selected),selected)
        (self.code/'user-note.txt').write_text('do not overwrite',encoding='utf8')
        with self.assertRaises(W.Problem) as caught:
            D.bind_target(self.t['id'],selected)
        self.assertEqual(caught.exception.code,'target_changed')

    def test_coverage_requires_requirement_id_and_empty_tool_view_is_not_pending(self):
        state=D.tool_inspect(self.execution_root,kind='code',target_id=self.r['id'],directory=self.session.dir)
        self.assertEqual(state['requirements'],[])
        self.assertEqual(state['requirementConfiguration'],'not_configured')
        self.assertFalse(state['gaps'])
        token=set_call(dict(sessionId=self.t['id'],runId='a'*32,actionId='b'*32,role='executor'))
        try:
            with self.assertRaises(W.Problem) as caught:
                D.tool_record(self.execution_root,dict(operation='coverage',expectedRevision=0,
                    kind='automatic',target=state['target'],actionId='c'*32,note='actual check'),directory=self.session.dir)
        finally: reset_call(token)
        self.assertEqual(caught.exception.code,'requirement_required')

    def test_native_prompt_uses_receipts_and_keeps_existing_user_requirements(self):
        self.session.task_input=dict(id='task',revision=1,kind='new')
        self.session.save(taskInput=self.session.task_input)
        core=EC.ExecutorCore(self.session,self.execution_root)
        self.assertIn('不必创建要求',core.instruction())
        D.mutate(self.t['id'],dict(id='d'*32,operation='requirement',expectedRevision=0,
            required=True,text='保留用户备注且运行原断言',target=dict(kind='code',id=self.r['id'])),self.sv)
        self.assertIn('已有明确必验项',core.instruction())
        self.assertFalse(D.executor_gate(self.session.dir,self.session.task_input)['ok'])
        before=D.load(D.storage(self.t['id'])[0])
        self.assertTrue(before['requirements'][0]['required'])
        registry=default_registry(self.execution_root);core.install(registry)
        self.assertEqual(D.load(D.storage(self.t['id'])[0]),before)

    def test_native_tool_surface_is_fixed_and_keeps_engineering_capabilities(self):
        from loop import plugins
        from loop.legacy_entry import draft_registry

        self.session.task_input=dict(id='task',revision=1)
        registry=default_registry(self.execution_root)
        before=len(json.dumps(registry.schemas(),ensure_ascii=False).encode('utf8'))
        with patch.object(plugins,'grants',return_value=[]):
            EC.ExecutorCore(self.session,self.execution_root).install(registry)
        schema=json.dumps(registry.schemas(),ensure_ascii=False)
        for name in ('edit_project_file','run_project_check','create_subtask','prepare_subtask_integration',
                     'read_context_history','transform_image','record_delivery_evidence','record_working_summary'):
            self.assertIsNotNone(registry.get(name),name)
        for name in ('install_catalog_plugin','call_plugin_tool','read_plugin_skill'):
            self.assertNotIn(name,registry.names(),name)
            self.assertIsNotNone(registry.get(name), f'{name}: old approvals retain their guarded handler')
        # Default L0 excludes continuous delegation tools. The draft entry still offers proposals.
        for name in ('propose_delegation', 'record_delegation_progress'):
            self.assertIsNone(registry.get(name), name)
        draft = draft_registry(self.execution_root, drafts=True)
        self.assertIn('propose_delegation', draft.names())
        self.assertIsNone(draft.get('record_delegation_progress'))
        self.assertLess(len(schema.encode('utf8')),before)
        with patch.object(plugins,'grants',return_value=[dict(skillsEnabled=True,remoteEnabled=True)]):
            self.assertEqual(json.dumps(registry.schemas(),ensure_ascii=False),schema)
            enabled=default_registry(self.execution_root)
            EC.ExecutorCore(self.session,self.execution_root).install(enabled)
        self.assertIn('call_plugin_tool',enabled.names())
        self.assertIn('read_plugin_skill',enabled.names())
        self.assertIsNone(enabled.get('propose_delegation'))
        with patch.object(plugins,'grants',side_effect=W.Problem('plugin_store','unreadable grant',409)):
            denied=default_registry(self.execution_root)
            EC.ExecutorCore(self.session,self.execution_root).install(denied)
        self.assertNotIn('call_plugin_tool',denied.names())
        self.assertIsNotNone(denied.get('run_project_check'))


if __name__=='__main__': unittest.main(verbosity=2)
