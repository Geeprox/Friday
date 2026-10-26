import unittest, asyncio, time, json
from unittest.mock import AsyncMock
from homeplane.embodied.local_intent import household_plan
from homeplane.embodied.intent import parse_plan
from homeplane.embodied.home_service import HomeService


class LocalIntentTests(unittest.TestCase):
    def test_direct_named_commands_and_word_order(self):
        for text, action in [
            ("Friday，打开窗帘", "open"),
            ("帮我打开客厅窗帘", "open"),
            ("请把客厅的梦幻帘拉开一下", "open"),
            ("客厅梦幻帘关上吧", "close"),
            ("关窗帘", "close"),
            ("帮我把窗帘打开", "open"),
        ]:
            p = household_plan(text, True)
            self.assertIsNotNone(p, text)
            self.assertEqual(p["action"], action, text)
            parse_plan(json.dumps(p))

    def test_no_attention_and_no_wake_never_admits(self):
        self.assertIsNone(household_plan("打开窗帘", False))
        self.assertEqual(household_plan("Friday打开窗帘", False)["action"], "open")

    def test_negation_does_not_invert_into_device_action(self):
        for text in ["别关窗帘", "不要打开客厅窗帘", "请先别关窗帘", "暂时不要关闭梦幻帘"]:
            p = household_plan(text, True)
            self.assertIsNotNone(p, text)
            self.assertEqual(p["route"], "no_op")
            self.assertIsNone(p["action"])

    def test_questions_quotes_conditions_schedules_compounds_and_other_devices_fall_through(self):
        for text in [
            "如果打开窗帘会怎么样",
            "他说打开窗帘",
            "打开窗帘了吗",
            "明天打开窗帘",
            "不要打开窗帘之后又关上",
            "打开窗帘然后关上",
            "先打开窗帘再查股票",
            "别关窗帘，打开它",
            "不是打开窗帘，是关窗帘",
            "我没说打开窗帘",
            "客厅灯打开",
            "帮我查一下今天股市怎么样",
            "把客厅拖鞭炸开",
            "打开主卧窗帘",
        ]:
            self.assertIsNone(household_plan(text, True), text)


class FastPathTests(unittest.IsolatedAsyncioTestCase):
    async def test_admitted_simple_command_skips_codex_but_query_uses_it(self):
        h = HomeService()
        h.selected = h.armed = True
        h.last_video = time.monotonic()
        h.request = AsyncMock(
            return_value={
                "text": "打开客厅窗帘",
                "address": {"admitted": True, "attention": True},
                "laya": {"choice": "指令或任务请求"},
            }
        )
        h.attention.latest = {3: {}}
        h.command = AsyncMock(return_value={"acknowledged": True})
        h.speaker.say = AsyncMock()
        await h.transcribe(b"1234", 3, time.monotonic(), True)
        self.assertEqual([c.args[0] for c in h.request.call_args_list], ["transcribe"])
        self.assertEqual(h.command.call_args.args[0], "open")
        self.assertEqual(h.state["intent_provider"], "local_rules")

    async def test_complex_query_is_delegated_and_no_wake_blocks_both_paths(self):
        h = HomeService()
        h.selected = h.armed = True
        h.last_video = time.monotonic()
        h.attention.latest = {3: {}}
        h.command = AsyncMock()
        h.speaker.say = AsyncMock()
        h.request = AsyncMock(
            side_effect=[
                {
                    "text": "查一下今天股市怎么样",
                    "address": {"admitted": True, "attention": True},
                    "laya": {"choice": "不确定"},
                },
                {
                    "type": "task_result",
                    "simulated": False,
                    "provider": "codex_app_server",
                    "plan": {
                        "is_command": True,
                        "route": "needs_capability",
                        "device": None,
                        "action": None,
                        "reply": "需要接入实时数据。",
                    },
                },
            ]
        )
        await h.transcribe(b"1234", 3, time.monotonic(), True)
        self.assertEqual([c.args[0] for c in h.request.call_args_list], ["transcribe", "delegate"])
        h.command.assert_not_awaited()
        h.request = AsyncMock(
            return_value={
                "text": "打开客厅窗帘",
                "address": {"admitted": False, "reason": "need_wake_word_without_attention"},
            }
        )
        await h.transcribe(b"1234", None, time.monotonic(), False)
        self.assertEqual(h.request.await_count, 1)
        h.command.assert_not_awaited()

    async def test_negation_neither_calls_codex_nor_actuator(self):
        h = HomeService()
        h.selected = h.armed = True
        h.last_video = time.monotonic()
        h.request = AsyncMock(
            return_value={"text": "别关窗帘", "address": {"admitted": True, "attention": True}, "laya": {}}
        )
        h.attention.latest = {3: {}}
        h.command = AsyncMock()
        h.speaker.say = AsyncMock()
        await h.transcribe(b"1234", 3, time.monotonic(), True)
        h.command.assert_not_awaited()
        self.assertEqual(h.state["last_route"], "no_op")
        self.assertEqual(h.request.await_count, 1)


if __name__ == "__main__":
    unittest.main()
