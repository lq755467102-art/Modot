"""L0 input budget checks at the model request boundary."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from loop import context_manager as C
from loop.context_inspection import request_owner
from loop.provider import Msg
from tools.context_budget_fixture import request_fixture


class InputBudgetTests(unittest.TestCase):
    def setUp(self):
        self.session, self.provider, self.events = request_fixture(self)
        C.bind_capacity(self.session, self.provider, 4096)

    def test_fixed_overflow_names_the_source_before_any_model_request(self):
        messages = [Msg("system", "Mandatory policy " * 1800), Msg("user", "Reply OK.")]
        with request_owner(self.session, "executor"), patch.object(self.provider, "_iter_lines") as wire:
            with self.assertRaises(C.ContextBlocked):
                self.provider.chat_stream(messages, [])
        wire.assert_not_called()
        blocked = next(row for row in self.events if row["act"] == "context_blocked")
        self.assertIn("未发送", blocked["reason"])
        self.assertIn("系统指令", blocked["reason"])
        self.assertEqual(blocked["inputBudget"]["inputLimit"], 3840)
        self.assertEqual(blocked["inputBudget"]["reason"], "fixed")
        self.assertEqual(blocked["inputBudget"]["history"], 0)

    def test_current_input_overflow_does_not_trim_the_users_message(self):
        text = "user material " * 900
        messages = [Msg("system", "Mandatory policy"), Msg("user", text)]
        with request_owner(self.session, "executor"), patch.object(self.provider, "_iter_lines") as wire:
            with self.assertRaises(C.ContextBlocked):
                self.provider.chat_stream(messages, [])
        wire.assert_not_called()
        blocked = next(row for row in self.events if row["act"] == "context_blocked")
        self.assertEqual(blocked["inputBudget"]["reason"], "current")
        self.assertGreater(blocked["inputBudget"]["current"], 3840)
        self.assertEqual(blocked["inputBudget"]["decisionMethod"], "unmeasured_input_bound")
        self.assertEqual(messages[-1].content, text)
        self.assertFalse(any(row["act"] == "context_compaction" for row in self.events))

    def test_unknown_model_window_still_prepares_an_ordinary_request(self):
        C.bind_capacity(self.session, self.provider, 0)
        self.session.executor_core = SimpleNamespace(
            budget_base_tools=set(), budget_groups={"skills": {"list_skills"}},
            budget_required_tools=set(), budget_requested_groups=set(),
        )
        messages = [Msg("system", "Mandatory rules"), Msg("user", "Reply OK.")]
        with request_owner(self.session, "executor"):
            prepared = C.prepare(self.provider, self.provider._body(messages, []))
        self.assertIsNone(prepared.input_budget["modelWindow"])
        self.assertIsNone(prepared.input_budget["inputLimit"])
        self.assertEqual(prepared.input_budget["deferred"], [])
        self.assertEqual(prepared["messages"][-1]["content"], "Reply OK.")

    def test_requested_tool_group_that_does_not_fit_is_named(self):
        self.session.executor_core = SimpleNamespace(
            budget_base_tools=set(), budget_groups={"debug": {"extra_debug"}},
            budget_required_tools=set(), budget_requested_groups={"debug"},
        )
        tools = [dict(name="extra_debug", description="Definition " * 1200,
                      parameters=dict(type="object"))]
        with request_owner(self.session, "executor"), patch.object(self.provider, "_iter_lines") as wire:
            with self.assertRaises(C.ContextBlocked):
                self.provider.chat_stream([Msg("system", "Mandatory policy"), Msg("user", "Continue.")], tools)
        wire.assert_not_called()
        blocked = next(row for row in self.events if row["act"] == "context_blocked")
        self.assertIn("debug", blocked["reason"])
        self.assertEqual(blocked["inputBudget"]["requestedGroups"], ["debug"])
        self.assertGreater(blocked["inputBudget"]["sources"]["extraTools"], 3840)

    def test_oversized_optional_catalogues_defer_at_the_provider_boundary(self):
        self.session.executor_core = SimpleNamespace(
            budget_base_tools={"use_tool_group", "load_skill"},
            budget_groups={"skills": {"list_skills"}, "plugins": {"list_plugins"},
                           "debug": {"extra_debug"}},
            budget_required_tools={"use_tool_group", "load_skill"},
            budget_requested_groups=set(),
        )
        skills = "## 可用技能\n" + "- `trace`：" + "optional skill detail " * 1600
        plugins = "## 本轮插件组件\n" + "optional plugin detail " * 1200
        worker_method = "Pinned plugin worker method. " * 10
        self.session.context_sources = [
            dict(kind="skills", text=skills), dict(kind="plugins", text=plugins),
        ]
        messages = [Msg("system", f"Mandatory rules\n\n{skills}\n\n{plugins}\n\n{worker_method}",
                        context_parts=(("plugins", worker_method),)),
                    Msg("user", "Use trace, then reply OK.")]
        names = ("use_tool_group", "load_skill", "extra_debug")
        tools = [dict(name=name, description="optional schema " * 700
                      if name == "extra_debug" else "Use the selected capability.",
                      parameters=dict(type="object")) for name in names]
        sent = []

        def lines(request, should_stop=None):
            sent.append(json.loads(request.data))
            yield b'data: {"choices":[{"delta":{"content":"OK"},"finish_reason":"stop"}]}'
            yield b'data: [DONE]'

        with request_owner(self.session, "executor"), \
                patch.object(self.provider, "_iter_lines", side_effect=lines), \
                patch("loop.context_inspection.capture"):
            self.assertEqual(self.provider.chat_stream(messages, tools).text, "OK")
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["messages"][-1]["content"], messages[-1].content)
        self.assertIn("Mandatory rules", sent[0]["messages"][0]["content"])
        self.assertIn("trace", sent[0]["messages"][0]["content"])
        self.assertIn("list_skills", sent[0]["messages"][0]["content"])
        self.assertIn("list_plugins", sent[0]["messages"][0]["content"])
        self.assertIn(worker_method, sent[0]["messages"][0]["content"])
        self.assertEqual([t["function"]["name"] for t in sent[0]["tools"]],
                         ["use_tool_group", "load_skill"])

    def test_large_earlier_runtime_update_still_reaches_history_policy(self):
        outdated = "[context-update:executor-work:old]\n" + "old state " * 1400
        current = "[context-update:executor-work:new]\ncurrent state"
        messages = [Msg("system", "Mandatory rules"),
                    Msg("user", "Earlier topic"),
                    Msg("user", outdated, context_parts=(("runtimeUpdate", outdated),)),
                    Msg("assistant", "Earlier answer"),
                    Msg("user", "Current task"),
                    Msg("user", current, context_parts=(("runtimeUpdate", current),))]
        with request_owner(self.session, "executor"), \
                patch("loop.context_view.compact", side_effect=C.ContextBlocked("fixture reached")) as compact:
            with self.assertRaisesRegex(C.ContextBlocked, "fixture reached"):
                C.prepare(self.provider, self.provider._body(messages, []))
        compact.assert_called_once()
        budget = next(row["before"]["budget"] for row in self.events if row["act"] == "context_compaction")
        self.assertGreater(budget["history"], 4096)
        self.assertLess(budget["fixed"] + budget["current"], 3840)
        error = next(row for row in self.events if row["act"] == "context_blocked")
        self.assertEqual(error["inputBudget"], budget)

    def test_interface_recovery_respects_smaller_reported_window_and_retries_once(self):
        self.session.context_user_limit = 32768
        C.bind_capacity(self.session, self.provider, 1048576)
        self.session.executor_core = SimpleNamespace(
            budget_base_tools=set(), budget_groups={"skills": {"list_skills"}},
            budget_required_tools=set(), budget_requested_groups=set(),
        )
        skill = "## 可用技能\n- `trace`：" + "long description " * 650
        self.session.context_sources = [dict(kind="skills", text=skill)]
        messages = [Msg("system", "Mandatory rules\n" + skill), Msg("user", "Reply OK.")]
        sent = []

        def lines(request, should_stop=None):
            sent.append(json.loads(request.data))
            # The second rejection reports a further reduction and offers
            # nominal room for a smaller output, which mustn't prompt a third call.
            window, tokens = (8192, 8900) if len(sent) == 1 else (4096, 4000)
            payload = dict(error=dict(code="context_length_exceeded", context_window=window,
                                      input_tokens=tokens))
            yield ("data: " + json.dumps(payload)).encode()

        with request_owner(self.session, "executor"), \
                patch.object(self.provider, "_iter_lines", side_effect=lines), \
                patch("loop.context_inspection.capture"):
            with self.assertRaises(C.ContextBlocked):
                self.provider.chat_stream(messages, [])
        self.assertEqual(len(sent), 2)
        self.assertNotIn(skill, sent[-1]["messages"][0]["content"])
        self.assertEqual(sent[0]["messages"][-1], sent[-1]["messages"][-1])
        blocked = next(row for row in self.events if row["act"] == "context_blocked")
        self.assertEqual(blocked["inputBudget"]["inputLimit"], 3840)
        self.assertEqual(blocked["inputBudget"]["providerInput"], 4000)
        self.assertEqual(blocked["inputBudget"]["stage"], "provider")


if __name__ == "__main__":
    unittest.main()
