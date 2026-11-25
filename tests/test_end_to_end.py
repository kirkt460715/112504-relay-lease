"""用真实 partner_stub 跑通夹具里的完整联调叙事。

对端在独立线程运行，业务侧通过真实 HTTP 客户端收发，验证：
commit_disconnect 对账、rejected 处理、替代渠道补播、整组停、
撤警缺口视图以及重启保持。
"""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread

from partner_stub import PartnerServer

from dispatch_backend.clock import Clock
from dispatch_backend.partner import PartnerClient
from dispatch_backend.registry import load_registry
from dispatch_backend.service import DispatchService
from dispatch_backend.storage import Store

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"
T0 = "2028-07-03T12:45:00+08:00"
T_RETURN = "2028-07-03T13:01:00+08:00"
T_WITHDRAW = "2028-07-03T13:05:00+08:00"
T_ALT = "2028-07-03T13:20:00+08:00"


class FixedClock(Clock):
    def __init__(self, value: str):
        from dispatch_backend.clock import parse_at

        self.moment = parse_at(value)

    def now(self):
        return self.moment

    def set(self, value: str):
        from dispatch_backend.clock import parse_at

        self.moment = parse_at(value)


class EndToEndTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        journal = Path(self.tmp.name) / "partner-journal.sqlite3"
        exchanges = FIXTURES / "partner-exchanges.json"
        self.partner_server = PartnerServer(("127.0.0.1", 0), exchanges, journal)
        self.addCleanup(self.partner_server.server_close)
        self.thread = Thread(
            target=self.partner_server.serve_forever,
            kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()
        self.addCleanup(self.partner_server.shutdown)
        self.addCleanup(self.thread.join, 2)

        self.clock = FixedClock(T0)
        self.registry = load_registry(FIXTURES / "registers.json")
        self.db = Path(self.tmp.name) / "dispatch.sqlite3"
        self.store = Store(self.db)
        self.client = PartnerClient(
            f"http://127.0.0.1:{self.partner_server.server_port}", timeout=3)
        self.service = DispatchService(self.store, self.registry, self.client, self.clock)
        self._bootstrap()

    def _bootstrap(self):
        s = self.service
        s.set_town_online("county", "north", False, at=T0)
        s.set_town_online("county", "west", True, at=T0)
        s.create_delegation(
            "county", "del-5", "north", "west", ["ridge", "ford"],
            ["speaker-1", "backup-sms", "speaker-3"],
            T0, "2028-07-03T14:00:00+08:00", at=T0)
        s.issue_warning("county", "flood-7", ["ridge", "ford"], at=T0)
        s.issue_warning("county", "slide-4", ["ridge"], at=T0)

    def test_full_narrative_against_real_partner(self):
        s = self.service

        # 1) 喇叭组启动 flood-7：接单后断链，保持 sent_unknown
        start = s.dispatch("west", "speaker-1", "start", "flood-7",
                           command_id="cmd-flood-start", at=T0)
        self.assertEqual(start["state"], "sent_unknown")
        self.assertIsNone(start["remote_id"])

        # 2) 同 ID 对账拿到接单回报（remote_id speaker-job-27）
        reconciled = s.reconcile("west", "cmd-flood-start", at=T0)
        self.assertEqual(reconciled["state"], "accepted_pending")
        self.assertEqual(reconciled["remote_id"], "speaker-job-27")

        # 3) 迟到的成音监听（对端查询）确认实际播出
        obs = s.fetch_observation(
            "west", operation_id="obs-flood-audible",
            remote_id="speaker-job-27", kind="audible", at=T_WITHDRAW)
        self.assertTrue(obs["heard"])
        self.assertFalse(obs["stale"])
        self.assertEqual(s.get_command("cmd-flood-start")["state"], "active")

        # 4) backup-sms 的 slide-4 被对端拒单
        rejected = s.dispatch("west", "backup-sms", "start", "slide-4",
                              command_id="cmd-sms-slide", at=T0)
        self.assertEqual(rejected["state"], "rejected")
        self.assertEqual(rejected["last_error"], "temporarily_unavailable")

        # 4b) 原乡恢复接入，ford 交还，ridge 仍由邻乡负责
        s.set_town_online("county", "north", True, at=T_RETURN)
        s.return_village("north", "del-5", "ford", at=T_RETURN)

        # 5) 县台对 ford 撤 flood-7；此时整组在播且无替代，缺口必须可见
        plan = s.request_withdrawal("county", "flood-7", ["ford"], at=T_WITHDRAW)
        self.assertEqual(plan["pending"], ["ford"])
        self.assertEqual(plan["unaffected_villages"], ["ridge"])
        ford = next(v for v in plan["villages"] if v["village"] == "ford")
        self.assertTrue(ford["still_in_push_scope"])
        blocker = ford["blockers"][0]
        self.assertEqual(blocker["channel"], "speaker-1")
        self.assertEqual(blocker["state"], "active")

        # 6) 无替代时整组停被拒，明确列出未覆盖的（预警, 村）。
        #    ford 已撤警；ridge 上 flood-7、slide-4 仍有效且无确认替代。
        from dispatch_backend.errors import ServiceError

        with self.assertRaises(ServiceError) as caught:
            s.dispatch("west", "speaker-1", "stop-group",
                       command_id="cmd-stop-early", at=T_WITHDRAW)
        self.assertEqual(caught.exception.code, "unsafe_group_stop")
        uncovered = {(g["warning"], g["village"])
                     for g in caught.exception.detail["uncovered"]}
        self.assertEqual(uncovered, {("flood-7", "ridge"), ("slide-4", "ridge")})

        # 7) 替代渠道可用后，先补足 ridge 上仍有效的两条预警并确认成音
        self.clock.set(T_ALT)
        alt_flood = s.dispatch("west", "speaker-3", "start", "flood-7",
                               villages=["ridge"], command_id="cmd-alt-flood", at=T_ALT)
        self.assertEqual(alt_flood["remote_id"], "speaker-alt-7")
        s.fetch_observation("west", "obs-alt-flood", "speaker-alt-7",
                            "audible", at=T_ALT)
        alt_slide = s.dispatch("west", "speaker-3", "start", "slide-4",
                               command_id="cmd-alt-slide", at=T_ALT)
        self.assertEqual(alt_slide["remote_id"], "speaker-alt-8")
        s.fetch_observation("west", "obs-alt-slide", "speaker-alt-8",
                            "audible", at=T_ALT)

        # 8) 整组停放行，静默观察后 ford 撤警确认
        stop = s.dispatch("west", "speaker-1", "stop-group",
                          command_id="cmd-group-stop", at=T_ALT)
        self.assertEqual(stop["state"], "accepted_pending")
        s.fetch_observation("west", "obs-group-silent", "speaker-job-28",
                            "silent", at=T_ALT)
        plan = s.withdrawal_plan("flood-7")
        self.assertEqual(plan["confirmed"], ["ford"])
        self.assertEqual(plan["pending"], [])

        # 9) ridge 的两条预警仍在替代渠道在播，没有被局部调整带走
        board = s.village_board("ridge")
        live = {(w["warning"], c["channel"])
                for w in board["warnings"] for c in w["channels"]
                if c["state"] == "active"}
        self.assertEqual(live, {("flood-7", "speaker-3"), ("slide-4", "speaker-3")})
        self.assertEqual(board["current_duty"], "west")

        # 10) ford 已交还，值班方回到原乡 north
        self.assertEqual(s.village_board("ford")["current_duty"], "north")

    def test_restart_keeps_uncertain_command_without_promotion(self):
        s = self.service
        s.dispatch("west", "speaker-1", "start", "flood-7",
                   command_id="cmd-restart-case", at=T0)
        self.assertEqual(s.get_command("cmd-restart-case")["state"], "sent_unknown")

        # 断电重启：新进程、同 DB、对端保持运行
        new_store = Store(self.db)
        new_service = DispatchService(
            new_store, self.registry,
            PartnerClient(f"http://127.0.0.1:{self.partner_server.server_port}"),
            self.clock)
        cmd = new_service.get_command("cmd-restart-case")
        self.assertEqual(cmd["state"], "sent_unknown")
        board = new_service.village_board("ridge")
        session = board["warnings"][0]["channels"][0]
        self.assertTrue(session["uncertain"])
        # 重启后仍可对账成功
        reconciled = new_service.reconcile("west", "cmd-restart-case", at=T0)
        self.assertEqual(reconciled["state"], "accepted_pending")
        new_store.close()


if __name__ == "__main__":
    unittest.main()
