"""台账初始化（含幂等）与完整 CLI 装配冒烟。"""
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from dispatch_backend.bootstrap import seed_from_registers
from dispatch_backend.registry import load_registry
from tests.support import FakePartner, build_service

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


class BootstrapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.partner = FakePartner()
        self.service, self.store, self.registry, self.clock = build_service(self.tmp.name, self.partner)
        self.data = json.loads((FIXTURES / "registers.json").read_text(encoding="utf-8"))

    def test_seed_is_idempotent_and_matches_ledger(self):
        first = seed_from_registers(self.service, self.registry, self.data)
        self.assertEqual(set(first["warnings"]), {"flood-7", "slide-4"})
        self.assertEqual(first["delegations"], ["del-5"])
        # 再次执行不重复建单、不报错
        second = seed_from_registers(self.service, self.registry, self.data)
        self.assertEqual(second["warnings"], [])
        self.assertEqual(second["delegations"], [])

        view = self.service.get_delegation("del-5")
        # 授权建立时不含运行态交还；交还由值班流程办理
        self.assertEqual(view["returned"], [])
        self.assertEqual(sorted(view["active_villages"]), ["ford", "ridge"])
        # 撤警请求也不在启动时预载
        plan = self.service.withdrawal_plan("flood-7")
        self.assertEqual(plan["requested"], [])
        with self.store.tx() as conn:
            self.assertFalse(self.store.town_online(conn, "north"))
            self.assertTrue(self.store.town_online(conn, "west"))

    def test_registry_loads_alternate_route_with_availability_time(self):
        registry = load_registry(FIXTURES / "registers.json")
        alt = registry.channels["speaker-3"]
        self.assertEqual(alt.mode, "per_alert")
        self.assertEqual(alt.scope, ("ridge",))
        self.assertIsNotNone(alt.available_from)
        self.assertEqual(registry.channels["speaker-1"].mode, "whole_group")


if __name__ == "__main__":
    unittest.main()
