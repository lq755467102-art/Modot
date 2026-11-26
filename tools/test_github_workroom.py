"""Exercise page, checkout and GitHub App authority without real credentials."""

import os
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from loop import github_workroom as G, workbench as W


class GitHubWorkroomTests(unittest.TestCase):
    cid = "fixture-chat"
    tab = "aaaaaaaa-1111-4444-8888-bbbbbbbbbbbb"
    repository = "sample/example"
    url = "https://github.com/sample/example/pull/8"
    sha = "1" * 40
    package = {"id": "github", "revision": 2, "servers": [
        {"name": "github", "supported": True, "url": G.plugin_connections.GITHUB_RESOURCE},
    ], "connections": {"github": {"method": "github-device"}}}

    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.data = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        stack.enter_context(patch.dict(os.environ, HARNESS_DATA=str(self.data), HARNESS_GITHUB_WORKROOM_PAGE=""))
        self.choice = {"id": "choice", "repository": self.repository, "resourceId": "local"}
        self.binding = {"scopeRevision": "scope", "revision": "binding", "selected": self.choice}
        stack.enter_context(patch.object(G, "binding", side_effect=lambda _: self.binding))
        stack.enter_context(patch.object(G.projects, "conversation", return_value={"id": self.cid}))
        stack.enter_context(patch.object(G.projects, "scope", return_value={"revision": "scope"}))
        self.plugins = stack.enter_context(patch.object(G.plugins, "state", return_value={"plugins": {"github": self.package}}))
        self.app_grant = stack.enter_context(patch.object(G.plugins, "authorized", return_value=self.package))
        self.app_secret = stack.enter_context(patch.object(G.plugin_connections, "authorization", return_value="Bearer synthetic"))
        stack.enter_context(patch.object(G.permissions, "profile", return_value={"revision": 1}))
        stack.enter_context(patch.object(G.permissions, "authorize", side_effect=lambda action, validate_live: validate_live()))
        W.atomic_json(G._binding_path(self.cid), {"revision": "binding"})
        self.ref = {"id": self.tab, "version": 1}
        self.row = {"id": self.tab, "version": 1, "active": True, "status": "ready", "owner": self.cid,
                    "pluginId": "github", "pluginRevision": 2,
                    "page": {"repository": self.repository, "number": 8, "url": self.url}}
        self.publish()
        self.run = self.data / "runs" / self.cid
        self.run.mkdir(parents=True)
        self.request = G.CurrentPullRequest(SimpleNamespace(dir=self.run))
        self.request.captured = G.capture(self.cid, self.ref)

    def publish(self, **changes):
        self.row.update(changes)
        W.atomic_json(W.home() / "github-views" / (self.tab + ".json"), dict(self.row, updatedAt=time.time()*1000))

    def response(self, path, auth, *, max_bytes, private=False):
        if private and not auth:
            raise W.Problem("github_repository_access", "denied", 403)
        if "/pulls/" in path:
            return {"number": 8, "head": {"sha": self.sha}, "title": "Example PR", "body": "DO_NOT_PROJECT_PR_BODY",
                    "base": {"repo": {"full_name": self.repository, "private": private, "id": 7}}}
        if "/check-runs?" in path:
            return {"total_count": 2, "check_runs": [
                {"head_sha": self.sha, "id": 11, "name": "CI example", "status": "completed", "conclusion": "failure",
                 "output": {"summary": "Check failed", "text": "DO_NOT_PROJECT_LOG"}},
                {"head_sha": self.sha, "id": 12, "name": "Queued job", "status": "queued", "conclusion": None},
            ]}
        return {"sha": self.sha, "state": "pending", "total_count": 0, "statuses": []}

    def test_remote_names_ignore_url_credentials_and_other_hosts(self):
        self.assertEqual(G.remote_repository("git@github.com:owner/name.git"), "owner/name")
        self.assertEqual(G.remote_repository("ssh://git@github.com/owner/name.git"), "owner/name")
        self.assertEqual(G.remote_repository("https://github.com/owner/name"), "owner/name")
        for bad in ["https://user:pass@github.com/owner/name", "https://github.com.other/owner/name",
                    "http://github.com/owner/name", "https://github.com/owner/name?state=credential",
                    "https://github.com/owner/.."]:
            self.assertIsNone(G.remote_repository(bad))

    def test_context_keeps_the_original_tab_and_explains_sign_in(self):
        self.assertIn("PR #8", self.request.instruction())
        for change in [{"version": 2}, {"active": False}, {"owner": "other-chat"}]:
            self.publish(**change)
            self.assertEqual(G.capture(self.cid, self.ref), {"status": "stale"})
            self.publish(version=1, active=True, owner=self.cid)
        self.publish(status="needs-user", page=None)
        self.assertEqual(G.capture(self.cid, self.ref), {"status": "needs-user"})

    def test_public_checks_are_bound_to_head_and_queue_state(self):
        with patch.object(G.plugin_connections, "github_get", side_effect=self.response) as api:
            result = self.request.read()
        self.assertEqual(result["headCommit"], self.sha)
        self.assertEqual([(r["id"], r["status"], r["conclusion"]) for r in result["checks"]],
                         [("11", "completed", "failure"), ("12", "queued", "")])
        self.assertTrue(all(call.args[1] is None for call in api.call_args_list))
        self.assertNotIn("DO_NOT_PROJECT", str(result))
        self.app_secret.assert_not_called()

    def test_private_read_requires_both_current_checkout_and_device_authorization(self):
        with patch.object(G.plugin_connections, "github_get",
                          side_effect=lambda *args, **kwargs: self.response(*args, private=True, **kwargs)) as api:
            result = self.request.read()
            self.assertTrue(result["private"])
            self.assertEqual(api.call_args_list[0].args[1], None)
            self.assertTrue(all(call.args[1] == "Bearer synthetic" for call in api.call_args_list[1:]))
            self.assertNotIn("Bearer", str(result))
            self.app_grant.assert_called_once_with(self.cid, "github", "remoteEnabled")
            self.binding["selected"] = None
            self.request.captured = G.capture(self.cid, self.ref)
            with self.assertRaisesRegex(W.Problem, "当前私库绑定"):
                self.request.read()

    def test_midflight_revocation_discards_result(self):
        def reply(*args, **kwargs):
            data = self.response(*args, **kwargs)
            self.plugins.return_value = {"plugins": {"github": dict(self.package, removed=True)}}
            return data

        with patch.object(G.plugin_connections, "github_get", side_effect=reply):
            with self.assertRaises(W.Problem) as failure:
                self.request.read()
        self.assertEqual(failure.exception.code, "github_access_changed")
        self.assertIsNone(self.request.result)

    def test_navigation_receipt_needs_both_original_request_and_native_confirmation(self):
        from loop import runtime

        target = self.url + "/checks?check_run_id=11"
        state = self.run / "github-navigation.json"
        record = {"id": "operation", "runId": "live", "state": "requested", "expiresAt": time.time()+12,
                  "captured": self.request.captured, "url": target, "privacy": False, "permissionRevision": 1}
        body = {"conversationId": self.cid, "id": "operation", "tabId": self.tab}
        with patch.object(runtime, "status", return_value={"active": True, "state": "running", "run_id": "live"}):
            for has_receipt in [False, True]:
                self.publish(version=1, receipt=None)
                W.atomic_json(state, record)
                with self.assertRaises(W.Problem):
                    G.navigation_response(dict(body, state="claimed", tabId="wrong-tab"))
                claim = G.navigation_response(dict(body, state="claimed"))
                self.assertEqual(claim["url"], target)
                self.publish(version=2, receipt={"id": "operation", "url": target, "version": 2} if has_receipt else None)
                if has_receipt:
                    self.assertTrue(G.navigation_response(dict(body, state="completed", outcome="opened"))["ok"])
                else:
                    with self.assertRaises(W.Problem) as failed:
                        G.navigation_response(dict(body, state="completed", outcome="opened"))
                    self.assertEqual(failed.exception.code, "github_page_unconfirmed")
        self.assertEqual(W.read(state)["ref"]["version"], 2)


if __name__ == "__main__":
    unittest.main()
