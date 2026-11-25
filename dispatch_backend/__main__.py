"""本地启动入口。

示例：
    python -m dispatch_backend --db ./run/dispatch.sqlite3 \
        --registers ./fixtures/registers.json --port 8080
"""
from __future__ import annotations

import argparse
from pathlib import Path

from .bootstrap import seed_from_registers
from .clock import Clock
from .fixed_clock import FixedClock
from .partner import PartnerClient
from .registry import load_registry
from .server import build_server
from .service import DispatchService
from .storage import Store

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    parser = argparse.ArgumentParser(description="山区应急广播跨乡调度后端")
    parser.add_argument("--db", type=Path, default=ROOT / "run" / "dispatch.sqlite3")
    parser.add_argument("--registers", type=Path, default=ROOT / "fixtures" / "registers.json")
    parser.add_argument("--partner", default="http://127.0.0.1:8765")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--seed", action="store_true", help="按台账初始化授权与预警（幂等）")
    parser.add_argument("--now", help="固定值班时钟（ISO-8601），便于演练历史台账；缺省用系统时间")
    args = parser.parse_args()

    args.db.parent.mkdir(parents=True, exist_ok=True)
    import json

    registry = load_registry(args.registers)
    store = Store(args.db)
    clock = FixedClock(args.now) if args.now else Clock()
    service = DispatchService(store, registry, PartnerClient(args.partner), clock)
    if args.seed:
        data = json.loads(args.registers.read_text(encoding="utf-8"))
        seeded = seed_from_registers(service, registry, data)
        print(f"初始化完成：{seeded}", flush=True)

    server = build_server(service, args.host, args.port)
    print(f"调度后端监听 http://{args.host}:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        store.close()


if __name__ == "__main__":
    main()
