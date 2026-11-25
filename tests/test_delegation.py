"""授权边界、交还与到期规则。"""
import unittest
from tempfile import TemporaryDirectory

from dispatch_backend.errors import PermissionError, ServiceError
from tests.support import FakePartner, build_service

WINDOW = ("2028-07-03T12:45:00+08:00", "2028-07-03T14:00:00+08:00")


class DelegationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.partner = FakePartner()
        self.service, self.store, self.registry, self.clock = build_service(self.tmp.name, self.partner)

    def _authorize(self, channels=("speaker-1", "backup-sms"), villages=("ridge", "ford")):
        self.service.set_town_online("county", "north", False, at=WINDOW[0])
        self.service.set_town_online("county", "west", True, at=WINDOW[0])
        self.service.create_delegation(
            "county", "del-1", "north", "west", list(villages), list(channels),
            WINDOW[0], WINDOW[1], at=WINDOW[0],
        )

    def test_agent_may_act_only_in_window_scope_and_channel_whitelist(self):
        self._authorize(channels=("backup-sms",))
        self.service.issue_warning("county", "flood-7", ["ridge"], at=WINDOW[0])
        self.partner.script("dispatch",
                            {"channel": "backup-sms", "action": "start", "alert": "flood-7"},
                            {"remote_id": "r1", "result": "accepted"})
        cmd = self.service.dispatch("west", "backup-sms", "start", "flood-7",
                                    command_id="c1", at=WINDOW[0])
        self.assertEqual(cmd["state"], "accepted_pending")

        # 未授权渠道
        with self.assertRaises(PermissionError) as caught:
            self.service.dispatch("west", "speaker-1", "start", "flood-7",
                                  command_id="c2", at=WINDOW[0])
        self.assertEqual(caught.exception.code, "channel_not_delegated")

        # 原乡对未交还村没有处置权（先恢复在线，才能走到处置权闸门）
        self.service.set_town_online("county", "north", True, at=WINDOW[0])
        with self.assertRaises(PermissionError) as caught:
            self.service.dispatch("north", "backup-sms", "start", "flood-7",
                                  command_id="c3", at=WINDOW[0])
        self.assertEqual(caught.exception.code, "not_current_duty")

    def test_returned_village_goes_back_to_owner_while_other_stays_delegated(self):
        self._authorize()
        self.service.return_village("north", "del-1", "ford", at="2028-07-03T13:01:00+08:00")
        self.clock.set("2028-07-03T13:02:00+08:00")
        with self.store.tx() as conn:
            duty_ford = self.service.duty_town(conn, "ford", self.clock.now())
            duty_ridge = self.service.duty_town(conn, "ridge", self.clock.now())
        self.assertEqual((duty_ford, duty_ridge), ("north", "west"))
        view = self.service.get_delegation("del-1")
        self.assertEqual(view["returned"], ["ford"])
        self.assertEqual(view["active_villages"], ["ridge"])

    def test_expiry_does_not_stop_playing_alerts_but_blocks_new_agent_commands(self):
        self._authorize()
        self.service.issue_warning("county", "flood-7", ["ridge"], at=WINDOW[0])
        self.partner.script("dispatch",
                            {"channel": "backup-sms", "action": "start", "alert": "flood-7"},
                            {"remote_id": "r1", "result": "accepted"})
        self.service.dispatch("west", "backup-sms", "start", "flood-7",
                              command_id="c1", at="2028-07-03T13:30:00+08:00")
        self.service.record_observation(
            "west", "r1", "audible", True, "2028-07-03T13:31:00+08:00",
            at="2028-07-03T13:31:00+08:00")

        # 越过授权有效期
        self.clock.set("2028-07-03T14:05:00+08:00")
        board = self.service.village_board("ridge")
        self.assertEqual(board["current_duty"], "north")
        # 正在播放的预警仍然在看板上，未被到期动作停掉
        self.assertEqual(board["warnings"][0]["status"], "active_warning")
        self.assertEqual(board["warnings"][0]["channels"][0]["state"], "active")

        with self.assertRaises(PermissionError) as caught:
            self.service.dispatch("west", "backup-sms", "start", "flood-7",
                                  command_id="c2", at="2028-07-03T14:05:00+08:00")
        self.assertEqual(caught.exception.code, "not_current_duty")

    def test_offline_town_cannot_dispatch(self):
        self._authorize()
        self.service.issue_warning("county", "flood-7", ["ridge"], at=WINDOW[0])
        self.service.set_town_online("county", "west", False, at=WINDOW[0])
        with self.assertRaises(ServiceError) as caught:
            self.service.dispatch("west", "backup-sms", "start", "flood-7",
                                  command_id="c9", at=WINDOW[0])
        self.assertEqual(caught.exception.code, "town_offline")


if __name__ == "__main__":
    unittest.main()
