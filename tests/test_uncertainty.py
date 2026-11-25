"""断链、迟到回报、重启保持与接管规则。"""
import unittest
from tempfile import TemporaryDirectory

from dispatch_backend.errors import PermissionError, ServiceError
from dispatch_backend.registry import Channel, Registry
from dispatch_backend.service import DispatchService
from dispatch_backend.storage import Store
from tests.support import FakePartner, build_service

T0 = "2028-07-03T12:45:00+08:00"
T1 = "2028-07-03T13:05:00+08:00"


class UncertaintyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.partner = FakePartner()
        self.service, self.store, self.registry, self.clock = build_service(self.tmp.name, self.partner)
        self.service.issue_warning("county", "flood-7", ["ridge"], at=T0)

    def test_commit_disconnect_stays_uncertain_until_observation(self):
        self.partner.script("dispatch",
                            {"channel": "speaker-1", "action": "start", "alert": "flood-7"},
                            {"remote_id": "sp-1", "result": "accepted"}, disconnect=True)
        cmd = self.service.dispatch("north", "speaker-1", "start", "flood-7",
                                    command_id="c1", at=T0)
        self.assertEqual(cmd["state"], "sent_unknown")
        self.assertIsNone(cmd["latest_observation"])

        # 用同一 ID 对账：对端日志显示已接单
        reconciled = self.service.reconcile("north", "c1", at=T0)
        self.assertEqual(reconciled["state"], "accepted_pending")
        self.assertEqual(reconciled["remote_id"], "sp-1")
        # accepted 仍不代表成音
        board = self.service.village_board("ridge")
        session = board["warnings"][0]["channels"][0]
        self.assertTrue(session["uncertain"])
        self.assertEqual(session["state"], "accepted_pending")

        # 迟到的成音回报确认实际播出
        obs = self.service.record_observation(
            "north", "sp-1", "audible", True, "2028-07-03T13:08:00+08:00", at=T1)
        self.assertFalse(obs["stale"])
        self.assertEqual(self.service.get_command("c1")["state"], "active")

    def test_reconcile_without_remote_record_keeps_unknown(self):
        self.partner.script("dispatch",
                            {"channel": "speaker-1", "action": "start", "alert": "flood-7"},
                            {"remote_id": "sp-2", "result": "accepted"}, disconnect=True)
        self.service.dispatch("north", "speaker-1", "start", "flood-7",
                              command_id="c2", at=T0)
        # 换一个对端没有的 ID 场景：直接清空假日志模拟无记录
        self.partner.journal.clear()
        result = self.service.reconcile("north", "c2", at=T0)
        self.assertEqual(result["state"], "sent_unknown")

    def test_restart_preserves_pending_without_promoting_or_dropping(self):
        self.partner.script("dispatch",
                            {"channel": "speaker-1", "action": "start", "alert": "flood-7"},
                            {"remote_id": "sp-3", "result": "accepted"}, disconnect=True)
        self.service.dispatch("north", "speaker-1", "start", "flood-7",
                              command_id="c3", at=T0)

        # 模拟断电重启：用同一个 SQLite 文件重建服务
        new_store = Store(self.store.path)
        new_service = DispatchService(new_store, self.registry, self.partner, self.clock)
        cmd = new_service.get_command("c3")
        self.assertEqual(cmd["state"], "sent_unknown")
        board = new_service.village_board("ridge")
        session = board["warnings"][0]["channels"][0]
        self.assertTrue(session["uncertain"])
        # 仍可能播出的范围被保留
        self.assertEqual(session["villages"], ["ridge"])
        new_store.close()

    def test_late_audible_after_confirmed_withdrawal_is_kept_but_marked_stale(self):
        self.partner.script("dispatch",
                            {"channel": "backup-sms", "action": "start", "alert": "flood-7"},
                            {"remote_id": "sms-1", "result": "accepted"})
        self.service.dispatch("north", "backup-sms", "start", "flood-7",
                              command_id="c4", at=T0)
        self.service.record_observation("north", "sms-1", "audible", True,
                                        "2028-07-03T12:50:00+08:00", at=T0)
        self.service.request_withdrawal("north", "flood-7", ["ridge"], at=T1)
        self.partner.script(
            "dispatch",
            {"channel": "backup-sms", "action": "stop", "alert": "flood-7"},
            {"remote_id": "sms-stop", "result": "accepted"})
        self.service.dispatch("north", "backup-sms", "stop", "flood-7",
                              command_id="c5", at=T1)
        self.service.record_observation("north", "sms-stop", "silent", False,
                                        "2028-07-03T13:10:00+08:00", at=T1)
        self.assertEqual(
            self.service.withdrawal_plan("flood-7")["confirmed"], ["ridge"])

        # 旧启动指令的迟到 audible：保留为 stale，不能把村挂回推送清单
        stale = self.service.record_observation(
            "north", "sms-1", "audible", True, "2028-07-03T13:12:00+08:00", at=T1)
        self.assertTrue(stale["stale"])
        self.assertEqual(stale["note"], "withdrawal_already_confirmed")
        plan = self.service.withdrawal_plan("flood-7")
        self.assertEqual(plan["confirmed"], ["ridge"])
        ridge = next(v for v in plan["villages"] if v["village"] == "ridge")
        self.assertFalse(ridge["still_in_push_scope"])
        # 回报本身仍可查
        observations = self.service.list_observations("c4")
        self.assertTrue(any(o["stale"] for o in observations))


class TakeoverTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.partner = FakePartner()
        self.service, self.store, self.registry, self.clock = build_service(self.tmp.name, self.partner)
        self.service.set_town_online("county", "north", False, at=T0)
        self.service.set_town_online("county", "west", True, at=T0)
        self.service.create_delegation(
            "county", "del-1", "north", "west", ["ridge"],
            ["speaker-1", "backup-sms"], T0, "2028-07-03T14:00:00+08:00", at=T0)
        self.service.issue_warning("county", "flood-7", ["ridge"], at=T0)
        self.partner.script("dispatch",
                            {"channel": "speaker-1", "action": "start", "alert": "flood-7"},
                            {"remote_id": "sp-x", "result": "accepted"}, disconnect=True)
        self.service.dispatch("west", "speaker-1", "start", "flood-7",
                              command_id="c-x", at=T0)

    def test_owner_takes_over_pending_and_late_agent_request_is_rejected(self):
        # 原乡恢复并交还村庄（此处直接由原乡恢复在线 + 交还）
        self.service.set_town_online("county", "north", True, at=T1)
        self.service.return_village("north", "del-1", "ridge", at=T1)

        # 旧代理的迟到派播：处置权已不在 west，拒绝且不会重新派播
        calls_before = len(self.partner.calls)
        with self.assertRaises(PermissionError) as caught:
            self.service.dispatch("west", "speaker-1", "start", "flood-7",
                                  command_id="c-late", at=T1)
        self.assertEqual(caught.exception.code, "not_current_duty")
        self.assertEqual(len(self.partner.calls), calls_before)

        # 原乡接管同一项未决处置并完成对账
        taken = self.service.takeover_pending("north", "c-x", at=T1)
        self.assertEqual(taken["handled_by"], "north")
        # west 已不能再操作该指令
        with self.assertRaises(PermissionError):
            self.service.retry_command("west", "c-x", at=T1)
        result = self.service.reconcile("north", "c-x", at=T1)
        self.assertEqual(result["state"], "accepted_pending")

        # 旧设备关于实际发生的迟到回报仍保留并归现任可见
        obs = self.service.record_observation(
            "north", "sp-x", "audible", True, "2028-07-03T13:08:00+08:00", at=T1)
        self.assertFalse(obs["stale"])
        board = self.service.village_board("ridge")
        self.assertEqual(board["current_duty"], "north")
        self.assertEqual(board["warnings"][0]["channels"][0]["state"], "active")

    def test_non_pending_command_cannot_be_taken_over(self):
        self.service.set_town_online("county", "north", True, at=T1)
        self.service.return_village("north", "del-1", "ridge", at=T1)
        self.service.reconcile("west", "c-x", at=T1)
        self.service.record_observation("west", "sp-x", "audible", True,
                                        "2028-07-03T12:50:00+08:00", at=T1)
        with self.assertRaises(ServiceError) as caught:
            self.service.takeover_pending("north", "c-x", at=T1)
        self.assertEqual(caught.exception.code, "command_not_pending")


if __name__ == "__main__":
    unittest.main()
