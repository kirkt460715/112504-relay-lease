"""边界与防护类规则。"""
import unittest
from tempfile import TemporaryDirectory

from dispatch_backend.errors import ServiceError
from tests.support import FakePartner, build_service

T0 = "2028-07-03T12:45:00+08:00"
T_ALT = "2028-07-03T13:20:00+08:00"


class EdgeCaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.partner = FakePartner()
        self.service, self.store, self.registry, self.clock = build_service(self.tmp.name, self.partner)
        self.service.issue_warning("county", "flood-7", ["ridge", "ford"], at=T0)

    def test_rejected_command_is_not_silently_replayed_but_redispatch_works(self):
        self.partner.script("dispatch",
                            {"channel": "backup-sms", "action": "start", "alert": "flood-7"},
                            {"remote_id": "r1", "result": "rejected",
                             "reason": "temporarily_unavailable"})
        first = self.service.dispatch("north", "backup-sms", "start", "flood-7",
                                      command_id="c1", at=T0)
        self.assertEqual(first["state"], "rejected")
        # 明确拒单不可静默重放（对端同 ID 只会返回原拒绝）
        with self.assertRaises(ServiceError) as caught:
            self.service.retry_command("north", "c1", at=T0)
        self.assertEqual(caught.exception.code, "command_not_retryable")
        # 恢复后值班员用新指令 ID 重新派播；更新假合作方脚本模拟渠道恢复
        self.partner.scripts = [
            item for item in self.partner.scripts if item["reply"].get("result") != "rejected"
        ]
        self.partner.script("dispatch",
                            {"channel": "backup-sms", "action": "start", "alert": "flood-7"},
                            {"remote_id": "r2", "result": "accepted"})
        redispatch = self.service.dispatch("north", "backup-sms", "start", "flood-7",
                                           command_id="c1-retry", at=T0)
        self.assertEqual(redispatch["state"], "accepted_pending")

    def test_alternate_channel_unavailable_before_its_time(self):
        with self.assertRaises(ServiceError) as caught:
            self.service.dispatch("north", "speaker-3", "start", "flood-7",
                                  villages=["ridge"], command_id="c2", at=T0)
        self.assertEqual(caught.exception.code, "channel_not_yet_available")
        # 到点后可用
        self.partner.script(
            "dispatch",
            {"channel": "speaker-3", "action": "start", "alert": "flood-7",
             "villages": ["ridge"]},
            {"remote_id": "r3", "result": "accepted"})
        ok = self.service.dispatch("north", "speaker-3", "start", "flood-7",
                                   villages=["ridge"], command_id="c3", at=T_ALT)
        self.assertEqual(ok["state"], "accepted_pending")

    def test_withdrawal_rejects_village_outside_warning(self):
        with self.assertRaises(ServiceError) as caught:
            self.service.request_withdrawal("county", "flood-7", ["bank"], at=T0)
        self.assertEqual(caught.exception.code, "village_not_under_warning")

    def test_dispatch_rejects_channel_outside_its_scope(self):
        with self.assertRaises(ServiceError) as caught:
            self.service.dispatch("north", "speaker-3", "start", "flood-7",
                                  villages=["ford"], command_id="c4", at=T_ALT)
        self.assertEqual(caught.exception.code, "channel_outside_scope")

    def test_stop_group_rejected_on_per_alert_channel(self):
        with self.assertRaises(ServiceError) as caught:
            self.service.dispatch("north", "backup-sms", "stop-group",
                                  command_id="c5", at=T0)
        self.assertEqual(caught.exception.code, "stop_group_requires_whole_group")

    def test_dispatch_idempotent_same_id_returns_same_command(self):
        self.partner.script("dispatch",
                            {"channel": "backup-sms", "action": "start", "alert": "flood-7"},
                            {"remote_id": "r4", "result": "accepted"})
        first = self.service.dispatch("north", "backup-sms", "start", "flood-7",
                                      command_id="shared-id", at=T0)
        second = self.service.dispatch("north", "backup-sms", "start", "flood-7",
                                       command_id="shared-id", at=T0)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.partner.calls), 1)  # 第二次未触达对端

    def test_unknown_actor_and_bad_time_rejected(self):
        with self.assertRaises(ServiceError) as caught:
            self.service.issue_warning("ghost", "w-x", ["ridge"], at=T0)
        self.assertEqual(caught.exception.code, "unknown_actor")
        # 朴素时间字符串（无时区）必须被拒绝
        with self.assertRaises(ServiceError):
            self.service.issue_warning("county", "w-y", ["ridge"],
                                       at="2028-07-03T12:00:00")

    def test_other_towns_warning_survives_unrelated_withdrawal(self):
        # bank 是 west 自己的村；给测试台账增加一个覆盖 bank 的 per_alert 渠道
        from dispatch_backend.registry import Channel

        self.registry.channels["bank-sms"] = Channel("bank-sms", ("bank",), "per_alert")
        self.service.issue_warning("county", "slide-4", ["bank"], at=T0)
        self.partner.script("dispatch",
                            {"channel": "bank-sms", "action": "start", "alert": "slide-4"},
                            {"remote_id": "r5", "result": "accepted"})
        self.service.dispatch("west", "bank-sms", "start", "slide-4",
                              command_id="c6", at=T0)
        self.service.record_observation("west", "r5", "audible", True,
                                        "2028-07-03T12:50:00+08:00", at=T0)
        # 对 flood-7 的处置不影响 west 自己 bank 村的 slide-4
        board = self.service.village_board("bank")
        self.assertEqual(board["warnings"][0]["warning"], "slide-4")
        self.assertEqual(board["warnings"][0]["channels"][0]["state"], "active")


if __name__ == "__main__":
    unittest.main()
