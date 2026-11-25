"""真实进程冒烟驱动：对运行中的后端 HTTP API 走完整个演练叙事。

用法：先启动 partner_stub 与 dispatch_backend，再运行本脚本。
"""
from http.client import HTTPConnection
import json
import sys

BASE = ("127.0.0.1", int(sys.argv[1]) if len(sys.argv) > 1 else 8080)


def call(method: str, path: str, data=None):
    conn = HTTPConnection(*BASE, timeout=5)
    try:
        conn.request(method, path, None if data is None else json.dumps(data),
                     {"Content-Type": "application/json"})
        response = conn.getresponse()
        return response.status, json.loads(response.read())
    finally:
        conn.close()


def check(label, condition, detail=""):
    print(("PASS" if condition else "FAIL"), "-", label, detail)
    if not condition:
        raise SystemExit(1)


# 1. 喇叭组启动：桩对该样例 commit_disconnect
status, cmd = call("POST", "/dispatch", {
    "actor": "west", "channel": "speaker-1", "action": "start",
    "alert": "flood-7", "id": "smoke-flood", "at": "2028-07-03T12:46:00+08:00"})
check("断链启动保持 sent_unknown", status == 200 and cmd["state"] == "sent_unknown", cmd["state"])

# 2. 同 ID 对账
status, cmd = call("POST", "/commands/smoke-flood/reconcile",
                   {"actor": "west", "at": "2028-07-03T12:47:00+08:00"})
check("对账拿到接单但未成音",
      cmd["state"] == "accepted_pending" and cmd["remote_id"] == "speaker-job-27",
      f'{cmd["state"]}/{cmd["remote_id"]}')

# 3. 迟到成音
status, obs = call("POST", "/observations/fetch", {
    "actor": "west", "id": "smoke-obs-1", "remote_id": "speaker-job-27",
    "kind": "audible", "at": "2028-07-03T13:08:00+08:00"})
check("迟到成音入账且非 stale", obs["heard"] is True and obs["stale"] is False)

# 4. 短信渠道拒单
status, cmd = call("POST", "/dispatch", {
    "actor": "west", "channel": "backup-sms", "action": "start",
    "alert": "slide-4", "id": "smoke-sms", "at": "2028-07-03T12:48:00+08:00"})
check("对端拒单落 rejected", cmd["state"] == "rejected"
      and cmd["last_error"] == "temporarily_unavailable", cmd["state"])

# 4b. 原乡恢复接入，ford 交还（ridge 仍由 west 负责）
status, _ = call("POST", "/towns/online",
                 {"actor": "county", "town": "north", "online": True,
                  "at": "2028-07-03T13:01:00+08:00"})
check("原乡恢复在线", status == 200)
status, _ = call("POST", "/delegations/del-5/return",
                 {"actor": "north", "village": "ford",
                  "at": "2028-07-03T13:01:00+08:00"})
check("ford 交还原乡", status == 200)

# 4c. 县台对 ford 撤 flood-7（局部撤警）
status, plan0 = call("POST", "/withdrawals", {
    "actor": "county", "warning": "flood-7", "villages": ["ford"],
    "at": "2028-07-03T13:05:00+08:00"})
check("撤警登记成功且 ford 待完成",
      status == 200 and plan0["pending"] == ["ford"], json.dumps(plan0, ensure_ascii=False))

# 5. 无替代时整组停被拒
status, body = call("POST", "/dispatch", {
    "actor": "west", "channel": "speaker-1", "action": "stop-group",
    "id": "smoke-stop-early", "at": "2028-07-03T13:06:00+08:00"})
uncovered = sorted((g["warning"], g["village"]) for g in body["detail"]["uncovered"])
check("整组停安全闸门拦截", status == 409 and body["error"] == "unsafe_group_stop"
      and uncovered == [("flood-7", "ridge"), ("slide-4", "ridge")], str(uncovered))

# 6. 替代渠道补足仍有效内容并确认成音
for payload in (
    {"actor": "west", "channel": "speaker-3", "action": "start", "alert": "flood-7",
     "villages": ["ridge"], "id": "smoke-alt-f", "at": "2028-07-03T13:20:00+08:00"},
    {"actor": "west", "channel": "speaker-3", "action": "start", "alert": "slide-4",
     "id": "smoke-alt-s", "at": "2028-07-03T13:20:00+08:00"},
):
    status, cmd = call("POST", "/dispatch", payload)
    check("替代渠道接单 " + payload["id"], status == 200 and cmd["state"] == "accepted_pending")
for obs_id, remote in (("smoke-of", "speaker-alt-7"), ("smoke-os", "speaker-alt-8")):
    status, obs = call("POST", "/observations/fetch", {
        "actor": "west", "id": obs_id, "remote_id": remote, "kind": "audible",
        "at": "2028-07-03T13:21:00+08:00"})
    check("替代成音确认 " + remote, obs["heard"] and not obs["stale"])

# 7. 整组停放行 + 静默
status, cmd = call("POST", "/dispatch", {
    "actor": "west", "channel": "speaker-1", "action": "stop-group",
    "id": "smoke-stop", "at": "2028-07-03T13:24:00+08:00"})
check("整组停接单", cmd["state"] == "accepted_pending")
status, obs = call("POST", "/observations/fetch", {
    "actor": "west", "id": "smoke-silent", "remote_id": "speaker-job-28",
    "kind": "silent", "at": "2028-07-03T13:25:00+08:00"})
check("静默观察入账", obs["heard"] is False)

# 8. 撤警计划
status, plan = call("GET", "/warnings/flood-7/withdrawal")
check("ford 撤警确认 / ridge 不受影响",
      plan["confirmed"] == ["ford"] and plan["pending"] == []
      and plan["unaffected_villages"] == ["ridge"], json.dumps(plan, ensure_ascii=False))

# 9. 村看板
status, ridge = call("GET", "/board/villages/ridge")
live = {(w["warning"], c["channel"]) for w in ridge["warnings"]
        for c in w["channels"] if c["state"] == "active"}
check("ridge 仍由 west 值班且替代渠道在播",
      ridge["current_duty"] == "west"
      and live == {("flood-7", "speaker-3"), ("slide-4", "speaker-3")}, str(live))
status, ford = call("GET", "/board/villages/ford")
check("ford 已交还 north 且撤警已确认",
      ford["current_duty"] == "north"
      and ford["warnings"][0]["status"] == "withdrawal_confirmed")

# 10. 迟到旧启动 audible：已撤警的 ford 不能被重新挂回，回报本身保留
status, late = call("POST", "/observations", {
    "actor": "west", "remote_id": "speaker-job-27", "kind": "audible",
    "heard": True, "observed_at": "2028-07-03T13:30:00+08:00",
    "at": "2028-07-03T13:30:00+08:00"})
check("迟到回报已保留", status == 200 and late["remote_id"] == "speaker-job-27")
status, plan = call("GET", "/warnings/flood-7/withdrawal")
ford_after = next(v for v in plan["villages"] if v["village"] == "ford")
check("ford 撤警结论不被迟到回报推翻",
      plan["confirmed"] == ["ford"] and ford_after["still_in_push_scope"] is False)
status, observations = call("GET", "/observations")
check("全部设备回报（含迟到）均可查",
      any(o["remote_id"] == "speaker-job-27" for o in observations["observations"]))

print("\n全部冒烟断言通过")
