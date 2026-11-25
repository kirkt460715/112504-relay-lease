"""分区撤警与整组停安全闸门。"""
import unittest
from tempfile import TemporaryDirectory

from dispatch_backend.errors import ServiceError
from tests.support import FakePartner, build_service

T0 = "2028-07-03T12:45:00+08:00"
T1 = "2028-07-03T13:05:00+08:00"
T_ALT = "2028-07-03T13:20:00+08:00"


class WithdrawalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.partner = FakePartner()
        self.service, self.store, self.registry, self.clock = build_service(self.tmp.name, self.partner)
        self.service.set_town_online("county", "north", False, at=T0)
        self.service.set_town_online("county", "west", True, at=T0)
        self.service.create_delegation(
            "county", "del-1", "north", "west", ["ridge", "ford"],
            ["speaker-1", "backup-sms", "speaker-3"], T0, "2028-07-03T14:00:00+08:00", at=T0)
        self.service.issue_warning("county", "flood-7", ["ridge", "ford"], at=T0)
        self.service.issue_warning("county", "slide-4", ["ridge"], at=T0)

    def _start_group(self, command_id, alert, remote_id, at=T0, disconnect=False):
        self.partner.script("dispatch",
                            {"channel": "speaker-1", "action": "start", "alert": alert},
                            {"remote_id": remote_id, "result": "accepted"},
                            disconnect=disconnect)
        return self.service.dispatch("west", "speaker-1", "start", alert,
                                     command_id=command_id, at=at)

    def test_partial_withdrawal_keeps_other_villages_and_other_alerts_live(self):
        self._start_group("c-flood", "flood-7", "sp-flood")
        self.service.record_observation("west", "sp-flood", "audible", True,
                                        "2028-07-03T12:50:00+08:00", at=T0)
        self._start_group("c-slide", "slide-4", "sp-slide")
        self.service.record_observation("west", "sp-slide", "audible", True,
                                        "2028-07-03T12:51:00+08:00", at=T0)

        plan = self.service.request_withdrawal("west", "flood-7", ["ford"], at=T1)
        # 撤警尚未完成：ford 仍在推送范围，缺口明确列出整组会话
        self.assertEqual(plan["pending"], ["ford"])
        self.assertEqual(plan["confirmed"], [])
        ford = next(v for v in plan["villages"] if v["village"] == "ford")
        self.assertTrue(ford["still_in_push_scope"])
        self.assertTrue(ford["blockers"])
        # 未撤警的 ridge 与另一预警 slide-4 不受影响
        ridge = next(v for v in plan["villages"] if v["village"] == "ridge")
        self.assertFalse(ridge["withdrawal_requested"])
        self.assertTrue(ridge["still_in_push_scope"])
        board = self.service.village_board("ridge")
        self.assertEqual({w["warning"] for w in board["warnings"]}, {"flood-7", "slide-4"})
        self.assertTrue(all(w["status"] == "active_warning" for w in board["warnings"]))

    def test_group_stop_blocked_until_alternate_confirmed_audible(self):
        self._start_group("c-flood", "flood-7", "sp-flood")
        self.service.record_observation("west", "sp-flood", "audible", True,
                                        "2028-07-03T12:50:00+08:00", at=T0)
        self._start_group("c-slide", "slide-4", "sp-slide")
        self.service.record_observation("west", "sp-slide", "audible", True,
                                        "2028-07-03T12:51:00+08:00", at=T0)
        self.service.request_withdrawal("west", "flood-7", ["ford"], at=T1)

        # 替代渠道未到可用时间
        with self.assertRaises(ServiceError) as caught:
            self.service.dispatch("west", "speaker-3", "start", "flood-7",
                                  villages=["ridge"], command_id="c-alt-early", at=T1)
        self.assertEqual(caught.exception.code, "channel_not_yet_available")

        # 无替代时整组停被安全闸门拒绝，缺口精确到（预警, 村）
        with self.assertRaises(ServiceError) as caught:
            self.service.dispatch("west", "speaker-1", "stop-group",
                                  command_id="c-stop-early", at=T1)
        self.assertEqual(caught.exception.code, "unsafe_group_stop")
        uncovered = {(g["warning"], g["village"]) for g in caught.exception.detail["uncovered"]}
        self.assertIn(("slide-4", "ridge"), uncovered)
        self.assertIn(("flood-7", "ridge"), uncovered)
        self.assertNotIn(("flood-7", "ford"), uncovered)  # ford 已撤警，不算仍有效

        # 在替代渠道补足仍有效内容并确认成音
        self.partner.script(
            "dispatch",
            {"channel": "speaker-3", "action": "start", "alert": "flood-7", "villages": ["ridge"]},
            {"remote_id": "alt-flood", "result": "accepted"})
        self.service.dispatch("west", "speaker-3", "start", "flood-7",
                              villages=["ridge"], command_id="c-alt-flood", at=T_ALT)
        self.service.record_observation("west", "alt-flood", "audible", True,
                                        "2028-07-03T13:21:00+08:00", at=T_ALT)
        self.partner.script(
            "dispatch",
            {"channel": "speaker-3", "action": "start", "alert": "slide-4"},
            {"remote_id": "alt-slide", "result": "accepted"})
        self.service.dispatch("west", "speaker-3", "start", "slide-4",
                              command_id="c-alt-slide", at=T_ALT)
        self.service.record_observation("west", "alt-slide", "audible", True,
                                        "2028-07-03T13:22:00+08:00", at=T_ALT)

        # 此时整组停放行
        self.partner.script("dispatch",
                            {"channel": "speaker-1", "action": "stop-group"},
                            {"remote_id": "sp-stop", "result": "accepted"})
        stop = self.service.dispatch("west", "speaker-1", "stop-group",
                                     command_id="c-stop", at=T_ALT)
        self.assertEqual(stop["state"], "accepted_pending")
        self.service.record_observation("west", "sp-stop", "silent", False,
                                        "2028-07-03T13:25:00+08:00", at=T_ALT)

        plan = self.service.withdrawal_plan("flood-7")
        self.assertEqual(plan["confirmed"], ["ford"])
        self.assertEqual(plan["pending"], [])
        # ridge 的两条预警在替代渠道仍在播
        board = self.service.village_board("ridge")
        channels = {c["channel"] for w in board["warnings"] for c in w["channels"]
                    if c["state"] == "active"}
        self.assertEqual(channels, {"speaker-3"})
        # 旧整组会话已被 stop-group 折叠，不会再显示在播
        for w in board["warnings"]:
            self.assertNotIn("speaker-1", [c["channel"] for c in w["channels"]
                                           if c["state"] == "active"])

    def test_per_alert_stop_only_touches_named_alert(self):
        self._start_group("c-flood", "flood-7", "sp-flood")
        self.service.record_observation("west", "sp-flood", "audible", True,
                                        "2028-07-03T12:50:00+08:00", at=T0)
        self.partner.script(
            "dispatch",
            {"channel": "backup-sms", "action": "start", "alert": "slide-4"},
            {"remote_id": "sms-slide", "result": "accepted"})
        self.service.dispatch("west", "backup-sms", "start", "slide-4",
                              command_id="c-sms", at=T0)
        self.service.record_observation("west", "sms-slide", "audible", True,
                                        "2028-07-03T12:52:00+08:00", at=T0)
        self.partner.script(
            "dispatch",
            {"channel": "backup-sms", "action": "stop", "alert": "flood-7"},
            {"remote_id": "sms-stop", "result": "accepted"})
        stop = self.service.dispatch("west", "backup-sms", "stop", "flood-7",
                                     command_id="c-sms-stop", at=T1)
        self.assertEqual(stop["action"], "stop")
        board = self.service.village_board("ridge")
        slide = next(w for w in board["warnings"] if w["warning"] == "slide-4")
        self.assertEqual(slide["status"], "active_warning")


if __name__ == "__main__":
    unittest.main()
