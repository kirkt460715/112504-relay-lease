"""值班台 HTTP API 黑盒测试（内存假合作方）。"""
import json
import unittest
from http.client import HTTPConnection
from tempfile import TemporaryDirectory
from threading import Thread

from dispatch_backend.server import build_server
from tests.support import FakePartner, build_service

T0 = "2028-07-03T12:45:00+08:00"


class HttpApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.partner = FakePartner()
        self.service, self.store, self.registry, self.clock = build_service(self.tmp.name, self.partner)
        self.clock.set(T0)
        self.server = build_server(self.service, "127.0.0.1", 0)
        self.addCleanup(self.server.server_close)
        self.thread = Thread(target=self.server.serve_forever,
                             kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.thread.join, 2)
        self.port = self.server.server_port

    def request(self, method: str, path: str, data=None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            conn.request(method, path,
                         None if data is None else json.dumps(data),
                         {"Content-Type": "application/json"})
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def test_health_board_and_error_envelope(self):
        status, body = self.request("GET", "/health")
        self.assertEqual((status, body), (200, {"status": "ok"}))
        status, body = self.request("GET", "/board/villages/nope")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "village_not_found")
        status, body = self.request("POST", "/dispatch", {"channel": "x"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "invalid_request")
        self.assertEqual(body["detail"]["error"], "actor_required")

    def test_duty_enforced_over_http_and_board_reflects_state(self):
        self.request("POST", "/towns/online",
                     {"actor": "county", "town": "north", "online": False, "at": T0})
        self.request("POST", "/towns/online",
                     {"actor": "county", "town": "west", "online": True, "at": T0})
        status, body = self.request("POST", "/delegations", {
            "actor": "county", "id": "del-1", "owner": "north", "delegate": "west",
            "villages": ["ridge"], "channels": ["backup-sms"],
            "valid_from": T0, "valid_until": "2028-07-03T14:00:00+08:00", "at": T0})
        self.assertEqual(status, 200, body)
        self.request("POST", "/warnings",
                     {"actor": "county", "id": "flood-7", "villages": ["ridge"], "at": T0})

        # 无授权的原乡被拒（离线冲突 409；恢复在线后则是处置权 403）
        status, body = self.request("POST", "/dispatch", {
            "actor": "north", "channel": "backup-sms", "action": "start",
            "alert": "flood-7", "id": "c1", "at": T0})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "town_offline")
        self.request("POST", "/towns/online",
                     {"actor": "county", "town": "north", "online": True, "at": T0})
        status, body = self.request("POST", "/dispatch", {
            "actor": "north", "channel": "backup-sms", "action": "start",
            "alert": "flood-7", "id": "c1b", "at": T0})
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "not_current_duty")

        # 代理乡派播成功
        self.partner.script("dispatch",
                            {"channel": "backup-sms", "action": "start", "alert": "flood-7"},
                            {"remote_id": "r1", "result": "accepted"})
        status, body = self.request("POST", "/dispatch", {
            "actor": "west", "channel": "backup-sms", "action": "start",
            "alert": "flood-7", "id": "c2", "at": T0})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["state"], "accepted_pending")

        # 同 ID 重试幂等
        status, body2 = self.request("POST", "/dispatch", {
            "actor": "west", "channel": "backup-sms", "action": "start",
            "alert": "flood-7", "id": "c2", "at": T0})
        self.assertEqual(status, 200)
        self.assertEqual(body2["id"], "c2")

        # 看板反映待确认状态
        status, board = self.request("GET", "/board/villages/ridge")
        self.assertEqual(status, 200)
        self.assertEqual(board["current_duty"], "west")
        session = board["warnings"][0]["channels"][0]
        self.assertTrue(session["uncertain"])

        # 未知路由
        status, body = self.request("GET", "/nope")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
