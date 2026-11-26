"""Shared-root isolation, real tree selection, CAS and retained versions."""

import unittest
from unittest.mock import patch
from tools.test_conversations import ConversationFixture
from loop import workbench as W, memory_service as M


class Trees(ConversationFixture, unittest.TestCase):
    def test_ineligible_nodes_do_not_charge_their_layer(self):
        from loop.memory_tree import select

        active = dict(id="active", tree="facts", title="Keep", body="Applicable",
                      layer=["same layer"], priority=2, state="active", branches=[])
        nodes = {"active": active}
        for name, patch in {
            "retired": {"state": "retired"},
            "branch": {"branches": ["other"]},
            "denied": {},
        }.items():
            nodes[name] = {**active, "id": name, "body": "x" * 100, "priority": 1, **patch}
        row = dict(nodes=nodes, choices={})
        selected = select(row, branch="main", max_chars=40, allowed=lambda n: n["id"] != "denied")
        self.assertEqual({n["id"]: n["reason"] for n in selected}, {
            "active": "selected", "retired": "state", "branch": "branch", "denied": "permission",
        })

    def setUp(self):
        super().setUp()
        from loop import habits as H
        for attr, path in [('path', self.root / 'owned-habits.md'), ('index_path', self.root / 'owned-habits.json')]:
            p = patch.object(H, attr, return_value=path)
            p.start()
            self.addCleanup(p.stop)
        self.p = W.handle(
            "POST", "/api/v2/projects", {}, dict(name="owned project"), self.sv
        )[1]
        self.a = self.call("POST", body=dict(projectId=self.p["id"]))[1]["id"]
        self.b = self.call("POST", body=dict(projectId=self.p["id"]))[1]["id"]
        self.other = self.call("POST")[1]["id"]

    def add(self, ident, **kw):
        return M.mutate(
            ident,
            dict(
                dict(
                    expectedRevision=M.target(ident)[0].load()["revision"],
                    title="Fact",
                    body="An observed project fact",
                    scope='project',
                ),
                **kw,
            ),
        )

    def view(self, ident):
        return M.resolve(ident, include_external_habits=False)

    def test_preview_does_not_create_conversation_or_store(self):
        before = len(W.registry()["threads"])
        store, _, _ = M.target(project_id=self.p["id"])
        self.assertFalse(store.path.exists())
        self.assertEqual(
            M.resolve(project_id=self.p["id"], include_external_habits=False)["nodes"], []
        )
        self.assertFalse(store.path.exists())
        self.assertEqual(before, len(W.registry()["threads"]))

    def test_same_project_shared_private_paths_not_shared(self):
        self.add(self.a)
        self.assertEqual(len(self.view(self.b)["nodes"]), 1)
        self.assertEqual(self.view(self.other)["nodes"], [])
        ts = W.registry()["threads"]
        self.assertNotEqual(ts[self.a]["workspaceId"], ts[self.b]["workspaceId"])

    def test_revision_conflict_and_restore(self):
        item = self.add(self.a)
        with self.assertRaises(W.Problem) as e:
            self.add(self.b, expectedRevision=0)
        self.assertEqual(e.exception.code, "memory_stale")
        store = M.target(self.a)[0]
        M.mutate(
            self.a,
            dict(
                id=item["id"],
                expectedRevision=1,
                title="Fact",
                body="New body changed for review",
            ),
        )
        restored_at = store.load()["nodes"][item["id"]]["updated"] + 100
        with patch("loop.memory_tree.time.time", return_value=restored_at):
            M.mutate(
                self.a,
                dict(
                    operation="configure",
                    expectedRevision=2,
                    id=item["id"],
                    restoreVersion=1,
                ),
            )
        self.assertEqual(
            store.load()["nodes"][item["id"]]["body"], "An observed project fact"
        )
        restored = store.load()["nodes"][item["id"]]
        self.assertEqual(restored["updated"], restored_at)

    def test_disabled_and_retired_nodes_not_implicitly_reopened(self):
        item = self.add(self.a)
        M.mutate(
            self.a,
            dict(
                operation="configure",
                expectedRevision=1,
                id=item["id"],
                on=False,
                mode="full",
            ),
        )
        self.assertEqual(self.view(self.a)["nodes"][0]["reason"], "not_selected")
        self.assertTrue(self.view(self.b)["nodes"][0]["selected"])
        self.assertTrue(
            M.resolve(project_id=self.p["id"], include_external_habits=False)["nodes"][0][
                "selected"
            ]
        )
        M.mutate(
            self.a,
            dict(
                operation="configure",
                expectedRevision=2,
                id=item["id"],
                state="retired",
            ),
        )
        self.assertFalse(self.view(self.b)["nodes"][0]["selected"])

    def test_layer_budget_stops_without_skipping_and_branch_conditions(self):
        self.add(self.a, layer=["A"], priority=10)
        self.add(self.a, layer=["B"], priority=20, key="large", id=None)
        store = M.target(self.a)[0]
        r = store.load()
        node = next(n for n in r["nodes"].values() if n["layer"] == ["B"])
        M.mutate(
            self.a,
            dict(
                expectedRevision=r["revision"],
                id=node["id"],
                title="Big",
                body="x" * 300,
                layer=["B"],
                priority=20,
                key="large",
            ),
        )
        self.add(self.a, layer=["C"], priority=30, key="small", branches=["main"])
        nodes = M.resolve(self.a, include_external_habits=False, max_chars=100, branch="main")[
            "nodes"
        ]
        self.assertEqual([n["layer"] for n in nodes if n["selected"]], [["A"]])

    def test_symlink_or_corrupt_store_fails_closed(self):
        store = M.target(self.a)[0]
        store.path.parent.mkdir(parents=True, exist_ok=True)
        store.path.write_text("{broken", encoding="utf8")
        with self.assertRaises(W.Problem):
            store.load()


if __name__ == "__main__":
    unittest.main()
