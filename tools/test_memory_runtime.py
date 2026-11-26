import json, unittest
from pathlib import Path
from unittest.mock import patch
from tools.test_memory_trees import Trees
from loop import (
    memory_service as M,
    memory_runtime as R,
    memory_learning as L,
    execution_policy as E,
    workbench as W,
    charter as C,
    habits as H,
)
from loop.session import Session
from loop.provider import OpenAICompatProvider, Msg


class Runtime(Trees):
    def setUp(self):
        super().setUp()
        cfg = dict(memory_tiers="habits=propose,rules=propose,facts=edit", roots=[])
        p = patch.object(C, "load_config", return_value=cfg)
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(H, "path", return_value=self.root / "owned-habits.md")
        p.start()
        self.addCleanup(p.stop)

    def owner(self, ident):
        directory = self.root / "runs" / ident
        directory.mkdir(parents=True, exist_ok=True)
        s = Session(directory)
        s.run_id = "a" * 32
        root = Path(W.workspace(W.registry()["threads"][ident]["workspaceId"])["path"])
        E.begin(s, dict(E.defaults(), crew="l0", modelCalls=10), root)
        p = OpenAICompatProvider(
            base_url="http://127.0.0.1:1/v1", api_key="owned", model="owned"
        )
        p.max_tokens = 1024
        s.request_context_limit = 50000
        return s, p

    def test_memory_wait_stops_serial_artifact_and_preserves_receipts(self):
        from loop.loop import run, pairing_errors, repair_msgs
        from loop.policy import Policy
        from loop.provider import Reply
        from loop.tools import default_registry

        a = self.add(self.a, title="Choice A", body="First choice", weight=60)
        b = self.add(self.a, title="Choice B", body="Second choice", weight=60)
        session = Session(self.root / "runs" / self.a)
        root = Path(W.workspace(W.registry()["threads"][self.a]["workspaceId"])["path"])
        registry = default_registry(root, mem=dict(facts="edit", rules="propose"))

        class Model:
            def chat(self, *_args, **_kw):
                return Reply(tool_calls=[
                    dict(id="first", name="publish_artifact", args={"title": "Before", "text": "kept"}),
                    dict(id="wait", name="compare_memory_choices",
                         args={"ids": [a["id"], b["id"]], "question": "Which choice?"}),
                    dict(id="last", name="publish_artifact", args={"title": "After", "text": "must not run"}),
                ])

        with patch("loop.hooks.run", return_value=[]):
            result = run("Task", Model(), registry, lambda: {"ok": True},
                         session=session, max_turns=1, policy=Policy("workspace", root=root, rules={}))
        self.assertEqual(result["status"], "needs_human")
        artifacts = W.registry()["threads"][self.a]["artifacts"]
        self.assertEqual(len(artifacts), 1)
        original = [Msg.load(row) for row in session.load()["msgs"]]
        self.assertFalse(pairing_errors(original))
        restored = repair_msgs(original)
        results = {row.tool_call_id: json.loads(row.content) for row in restored if row.role == "tool"}
        self.assertTrue(results["first"]["ok"])
        self.assertEqual(results["wait"]["options"], ["Choice A", "Choice B"])
        self.assertEqual(results["last"]["execution_state"], "cancelled_before_start")

    def test_completed_action_with_receipt_timeout_stays_unknown(self):
        from loop.loop import run
        from loop.policy import Policy
        from loop.provider import MockProvider
        from loop.tools import default_registry

        session = Session(self.root / "runs" / self.a)
        root = Path(W.workspace(W.registry()["threads"][self.a]["workspaceId"])["path"])
        model = MockProvider([{"tool": "publish_artifact", "args": {"title": "Saved", "text": "kept"}}])
        with patch("loop.hooks.run", return_value=[]), patch("loop.evidence.capture", side_effect=TimeoutError):
            run("Task", model, default_registry(root), lambda: {"ok": True},
                session=session, max_turns=1, policy=Policy("workspace", root=root, rules={}))
        receipt = next(row for row in session.load()["msgs"] if row["role"] == "tool")
        self.assertEqual(json.loads(receipt["content"])["execution_state"], "unknown")
        self.assertEqual(len(W.registry()["threads"][self.a]["artifacts"]), 1)

    def test_actual_requests_refresh_and_snapshots_remain_versioned(self):
        fact = self.add(self.a)
        s, p = self.owner(self.b)
        seen = []

        def once(req, should_stop=None):
            seen.append(json.loads(req.data))
            return dict(
                choices=[dict(message=dict(role="assistant", content="Observed"))],
                usage={},
            )

        with patch.object(p, "_once", side_effect=once):
            provider = E.provider(p, s, "executor")
            provider.chat([Msg("user", "Task")], [])
            old = M.requests(self.b)[0]["id"]
            self.assertIn("An observed project fact", str(seen[0]))
            memory_page = dict(ok=True, **M.model_page(self.b),
                               _harnessEvidence={"runId": s.run_id})
            messages = [Msg("user", "Task"),
                        Msg("assistant", tool_calls=[dict(id="memory", type="function",
                            function=dict(name="read_memory_tree", arguments="{}"))]),
                        Msg("tool", json.dumps(memory_page), tool_call_id="memory")]
            provider.chat(messages, [])
            self.assertEqual(json.loads(seen[1]["messages"][-1]["content"])["selectionVersion"],
                             memory_page["selectionVersion"])
            request = W.read(max((s.dir / "context-requests").glob("*.json"),
                                 key=lambda path: path.stat().st_mtime_ns))
            breakdown = request["capacity"]["breakdown"]
            self.assertGreater(breakdown["memoryFacts"], 0)
            self.assertGreater(breakdown["toolResults"], 0)
            self.assertEqual(sum(breakdown.values()), request["capacity"]["estimatedTotal"])
            store = M.target(self.a)[0]
            M.mutate(
                self.a,
                dict(
                    expectedRevision=store.load()["revision"],
                    id=fact["id"],
                    title="Fact",
                    body="New current fact body",
                ),
            )
            provider.chat(messages, [])
        self.assertIn("New current fact body", str(seen[2]))
        self.assertNotIn("An observed project fact", str(seen[2]))
        self.assertIn("旧结果", seen[2]["messages"][-1]["content"])
        self.assertEqual(
            M.read_snapshot(self.b, old)["nodes"][0]["body"], "An observed project fact"
        )
        self.assertEqual(W.read(s.execution_budget)["usedCalls"], 3)

    def test_mode_off_applies_to_real_request_without_deleting_tree(self):
        self.add(self.a)
        M.mutate(self.b, dict(operation="mode", expectedRevision=1, mode="off"))
        s, p = self.owner(self.b)
        b = R.prepare_body(
            s, "executor", dict(messages=[dict(role="user", content="Task")])
        )
        self.assertNotIn("An observed project fact", str(b))
        self.assertEqual(len(M.target(self.b)[0].load()["nodes"]), 1)

    def test_all_scoped_roles_receive_current_project_facts(self):
        self.add(self.a)
        s, p = self.owner(self.b)
        for role in ("executor", "planner", "reviewer"):
            b = R.prepare_body(
                s,
                role,
                dict(
                    messages=[
                        dict(role="system", content="Role scope"),
                        dict(role="user", content="Task"),
                    ]
                ),
            )
            self.assertIn("An observed project fact", str(b))
            self.assertTrue(R.capture(s, role, b)["loaded"])

    def test_near_weight_conflict_requires_human_and_does_not_repeat(self):
        a = self.add(self.a)
        b = self.add(self.a, key="other", weight=63)
        store = M.target(self.a)[0]
        result = M.mutate(
            self.a,
            dict(
                operation="compare",
                expectedRevision=2,
                ids=[a["id"], b["id"]],
                question="Choose a feasible approach",
            ),
        )
        self.assertTrue(result["needs_human"])
        self.assertIsNotNone(L.pending_guard(self.a))
        M.mutate(
            self.a,
            dict(
                operation="decide",
                expectedRevision=3,
                conflictId=result["conflict"]["id"],
                selected=a["id"],
            ),
        )
        self.assertIsNone(L.pending_guard(self.a))
        again = M.mutate(
            self.a,
            dict(
                operation="compare",
                expectedRevision=4,
                ids=[a["id"], b["id"]],
                question="Choose a feasible approach",
            ),
        )
        self.assertEqual(again["conflict"]["state"], "resolved")

    def test_unavailable_choices_do_not_create_or_keep_a_write_block(self):
        first = self.add(self.a, title="First choice", weight=60)
        second = self.add(self.a, title="Second choice", weight=60)
        compare = dict(operation="compare", ids=[first["id"], second["id"]], question="Choose")
        with patch.object(M, "current_branch", return_value="other"):
            scoped = self.add(self.a, title="Main only", weight=60, branches=["main"])
            with self.assertRaises(W.Problem) as denied:
                M.mutate(self.a, dict(compare, expectedRevision=3, ids=[first["id"], scoped["id"]]))
            self.assertEqual(denied.exception.code, "memory_denied")
        self.assertTrue(M.mutate(self.a, dict(compare, expectedRevision=3))["needs_human"])
        self.assertIsNotNone(L.pending_guard(self.a))
        M.mutate(self.a, dict(operation="configure", id=second["id"], on=False, mode="full", expectedRevision=4))
        self.assertIsNone(L.pending_guard(self.a))
        self.assertEqual(M.resolve(self.a)["conflicts"], [])
        with self.assertRaises(W.Problem) as denied:
            M.mutate(self.a, dict(compare, expectedRevision=5))
        self.assertEqual(denied.exception.code, "memory_denied")

    def test_repeated_reads_age_and_retries_do_not_establish_habit_eligibility(self):
        fact = self.add(self.a)
        store = M.target(self.a)[0]
        for _ in range(5):
            L.record_injection(self.a, "same-run", [fact["id"]])
        n = store.load()["nodes"][fact["id"]]
        n["created"] -= 86400 * 90
        self.assertEqual(len(n["evidence"]), 1)
        self.assertFalse(L.eligible(n)["eligible"])

    def test_model_cannot_forge_human_confirmation(self):
        a = self.add(self.a)
        with self.assertRaises(W.Problem):
            M.mutate(
                self.a,
                dict(
                    operation="evidence",
                    expectedRevision=1,
                    id=a["id"],
                    kind="human-confirmed",
                ),
                "model",
            )

    def test_age_injection_and_checks_do_not_prove_independent_use(self):
        from loop import code_service as CS
        import time

        with patch("loop.memory_tree.time.time", return_value=time.time() - 86400 * 10):
            fact = self.add(self.a)
        code = self.root / "owned-code"
        code.mkdir()
        resource = W.handle(
            "POST",
            "/api/v2/projects/" + self.p["id"] + "/resources",
            {},
            dict(path=str(code), kind="code"),
            self.sv,
        )[1]
        W.change_registry(
            lambda row: row["projects"][self.p["id"]]["resources"][
                resource["id"]
            ].update(permission="read")
        )
        W.change_registry(
            lambda row: row["threads"][self.a].update(resourceIds=[resource["id"]])
        )
        for index in range(2):
            run = str(index + 1) * 32
            oid = "check-" + str(index)
            CS.record(
                self.a,
                dict(
                    id=oid,
                    operation="check",
                    resourceId=resource["id"],
                    source=dict(runId=run),
                    state="completed",
                    result=dict(process=dict(state="completed", exitCode=0)),
                ),
            )
            row = M.target(self.a)[0].load()
            M.mutate(
                self.a,
                dict(
                    operation="evidence",
                    expectedRevision=row["revision"],
                    id=fact["id"],
                    kind="check",
                    operationId=oid,
                ),
            )
            L.record_injection(self.a, run, [fact["id"]])
        row = M.target(self.a)[0].load()
        self.assertFalse(L.eligible(row["nodes"][fact["id"]])["eligible"])
        M.mutate(
            self.a,
            dict(
                operation="evidence",
                expectedRevision=row["revision"],
                id=fact["id"],
                kind="human-confirmed",
            ),
        )
        row = M.target(self.a)[0].load()
        proposal = M.mutate(
            self.a,
            dict(operation="promote", expectedRevision=row["revision"], id=fact["id"]),
        )
        self.assertFalse(proposal["qualification"]["eligible"])
        self.assertFalse((self.root / "owned-habits.md").exists())
        self.assertFalse(proposal['promoted'])
        self.assertEqual(M.resolve(self.other, include_external_habits=False)["nodes"], [])

    def test_close_conflict_blocks_actual_write_tool_until_user_selects(self):
        from loop.tools import Tool, set_call, reset_call

        a = self.add(self.a)
        b = self.add(self.a, key="second", weight=64)
        store = M.target(self.a)[0]
        conflict = M.mutate(
            self.a,
            dict(
                operation="compare",
                expectedRevision=2,
                ids=[a["id"], b["id"]],
                question="Select one approach",
            ),
        )
        effects = []
        tool = Tool(
            "owned_write",
            "Fixture",
            {},
            lambda: effects.append(1),
            zone="offflow",
            kind="write",
        )
        token = set_call(dict(sessionId=self.a, runId="a" * 32))
        try:
            self.assertTrue(tool({})["needs_human"])
            self.assertEqual(effects, [])
            M.mutate(
                self.a,
                dict(
                    operation="decide",
                    expectedRevision=3,
                    conflictId=conflict["conflict"]["id"],
                    selected=a["id"],
                ),
            )
            tool({})
            self.assertEqual(effects, [1])
        finally:
            reset_call(token)

    def test_private_registry_exposes_only_authorized_local_memory_outputs(self):
        from loop.tools import default_registry
        from loop.policy import Policy

        root = Path(W.workspace(W.registry()["threads"][self.a]["workspaceId"])["path"])
        registry = default_registry(
            root, mem=dict(habits="propose", rules="propose", facts="edit")
        )
        tool = registry.get("organize_memory_fact")
        self.assertIsNotNone(tool)
        self.assertTrue(
            Policy("readonly", root, rules={}).check(tool.name, {}, kind=tool.kind)[0]
        )
        locked = default_registry(
            root, mem=dict(habits="none", rules="none", facts="read")
        )
        self.assertIsNone(locked.get("organize_memory_fact"))
        self.assertIsNotNone(locked.get("read_memory_tree"))
        from loop.tools import set_call, reset_call
        call_token = set_call(dict(sessionId=self.a, runId='a'*32, actionId='scoped-memory-read'))
        self.addCleanup(lambda:reset_call(call_token))
        empty = locked.get("read_memory_tree")({})
        self.assertTrue(empty.get("ok"), "a valid empty read must not emit tool.ok=false")
        self.assertEqual(empty["nodes"], [])
        self.add(self.a)
        populated = locked.get("read_memory_tree")({})
        self.assertTrue(populated.get("ok"))
        self.assertEqual(len(populated["nodes"]), 1)

    def test_concurrent_same_revision_has_one_writer(self):
        from concurrent.futures import ThreadPoolExecutor

        def write(index):
            try:
                return M.mutate(
                    self.a,
                    dict(
                        expectedRevision=0,
                        title="N" + str(index),
                        body="Fact " + str(index),
                    ),
                )
            except W.Problem as e:
                return e.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(write, [1, 2]))
        self.assertEqual(results.count("memory_stale"), 1)
        self.assertEqual(len(M.target(self.a, scope='session')[0].load()["nodes"]), 1)

    def test_old_version_checks_cannot_qualify_changed_fact(self):
        a = self.add(self.a)
        store = M.target(self.a)[0]
        M.mutate(
            self.a,
            dict(
                operation="evidence",
                expectedRevision=1,
                id=a["id"],
                kind="human-confirmed",
            ),
        )
        M.mutate(
            self.a,
            dict(
                expectedRevision=2, id=a["id"], title="Fact", body="Changed proposition"
            ),
        )
        n = store.load()["nodes"][a["id"]]
        self.assertFalse(L.eligible(n)["eligible"])
        self.assertTrue(any("泛化" in reason for reason in L.eligible(n)["reasons"]))

    def test_child_projection_uses_parent_memory_but_only_assigned_source_ids(self):
        import os
        from loop import subtasks as S, projects
        from loop.tools import set_call, reset_call

        docs = self.root / "docs"
        docs.mkdir()
        (docs / "note.txt").write_text("owned", encoding="utf8")
        resource = W.handle(
            "POST",
            "/api/v2/projects/" + self.p["id"] + "/resources",
            {},
            dict(path=str(docs), kind="documents"),
            self.sv,
        )[1]
        W.change_registry(
            lambda r: r["projects"][self.p["id"]]["resources"][resource["id"]].update(
                permission="read"
            )
        )
        W.change_registry(
            lambda r: r["threads"][self.a].update(resourceIds=[resource["id"]])
        )
        self.add(self.a)
        projects.snapshot(self.a)
        directory = self.sv._resolve_sid(self.a)
        run = W.RT.register(directory, os.getpid())["run_id"]
        W.atomic_json(
            directory / "execution-runs" / (run + ".json"),
            dict(runId=run, config=E.defaults(), usedCalls=1, byRole={}),
        )
        token = set_call(
            dict(sessionId=self.a, runId=run, actionId="a" * 32, tool="create_subtask")
        )
        try:
            task = S.create(
                self.a,
                dict(
                    request_key="memory-report",
                    goal="Read assigned facts",
                    resource_ids=[resource["id"]],
                    model_calls=3,
                    kind="report",
                ),
                self.sv,
                schedule=False,
            )
        finally:
            reset_call(token)
        S._prepare(task, self.sv)
        rows = M.resolve(task["childId"], include_external_habits=False)["nodes"]
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["selected"])
        self.assertIn("An observed project fact", rows[0]["body"])
        M.mutate(
            self.a,
            dict(
                operation="configure",
                expectedRevision=M.target(self.a)[0].load()["revision"],
                id=rows[0]["id"],
                on=False,
                mode="full",
            ),
        )
        self.assertFalse(
            M.resolve(task["childId"], include_external_habits=False)["nodes"][0]["selected"]
        )
        self.assertNotEqual(
            W.registry()["threads"][task["childId"]]["projectId"], self.p["id"]
        )
        W.change_registry(
            lambda r: r["projects"][self.p["id"]]["resources"][resource["id"]].update(
                permission="none"
            )
        )
        with self.assertRaises(W.Problem):
            M.resolve(task["childId"], include_external_habits=False)

    def test_legacy_habit_rename_preserves_workspace_weight_and_identity(self):
        path = self.root / "owned-habits.md"
        path.write_text(
            "# Habits\n## Style\n### Small changes\nPrefer compact changes.\n",
            encoding="utf8",
        )
        # Unadopted file entries are not advertised as current L0 memory.
        self.assertFalse(any(n['adapter'] == 'habits' for n in M.resolve(self.a)['nodes']))
        before = next(n for n in M.external_habits(M.target(self.a)[0].load()) if n["adapter"] == "habits")
        M.mutate(
            self.a,
            dict(
                operation="habit-weight",
                expectedRevision=0,
                id=before["id"],
                expectedVersion=before["version"],
                weight=85,
            ),
        )
        self.assertTrue(H.rename("Small changes", "Scoped changes")["ok"])
        after = next(n for n in M.external_habits(M.target(self.a)[0].load()) if n["adapter"] == "habits")
        self.assertEqual(after["id"], before["id"])
        self.assertEqual(after["weight"], 85)

    def test_cross_tree_conflict_policy_boundary_examples(self):
        cases = [
            (80, 79, True),
            (75, 70, True),
            (76, 70, False),
            (50, 90, False),
            (90, 50, False),
            (70, 70, True),
        ]
        for index, (habit_weight, fact_weight, ask) in enumerate(cases):
            store = M.target(self.a)[0]
            a = M.mutate(
                self.a,
                dict(
                    expectedRevision=store.load()["revision"],
                    tree="habits",
                    scope='project',
                    key="habit-" + str(index),
                    title="Prefer compatibility " + str(index),
                    body="Preserve the compatibility route",
                    weight=habit_weight,
                ),
            )
            b = M.mutate(
                self.a,
                dict(
                    scope='project',
                    expectedRevision=store.load()["revision"],
                    key="fact-" + str(index),
                    title="Use new interface " + str(index),
                    body="Current verified project interface supports the alternative",
                    weight=fact_weight,
                ),
            )
            result = M.mutate(
                self.a,
                dict(
                    operation="compare",
                    expectedRevision=store.load()["revision"],
                    ids=[a["id"], b["id"]],
                    question="Which feasible route for case " + str(index),
                ),
            )
            self.assertEqual(result["needs_human"], ask)
            if not ask:
                self.assertEqual(
                    result["conflict"]["selected"],
                    a["id"] if habit_weight > fact_weight else b["id"],
                )


# Policy and source-binding tests also exercise the private-conversation registry.
if __name__ == "__main__":
    unittest.main()
