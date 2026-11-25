# 调度后端运行与接口说明

县乡应急广播跨乡代理调度后端。纯 Python 3.11 标准库，本地单机运行，SQLite 落盘。

## 1. 启动

需要两个本地进程：对端协议桩（设备/合作方）和本调度后端。

```sh
# 终端 A：对端协议桩（可独立保持运行，后端重启不影响它）
python3 partner_stub.py --port 8765 --journal ./run/partner.sqlite3

# 终端 B：调度后端。--seed 按 fixtures/registers.json 幂等初始化授权与预警；
# --now 把值班时钟固定到演练时段，便于复现历史台账（缺省用系统当前时间）
python3 -m dispatch_backend \
  --db ./run/dispatch.sqlite3 \
  --partner http://127.0.0.1:8765 \
  --port 8080 --seed \
  --now "2028-07-03T13:05:00+08:00"
```

断电重启演练：直接用同一个 `--db` 再次启动即可。待确认指令、撤警结论、
授权交还记录都在 SQLite 中，重启既不会把"接单待确认"提升为成功，也不会丢失。

## 2. 角色与处置权

* `actor` 为值班乡镇标识（如 `north`、`west`）；`county` 是县台管理员，
  可办授权、在线状态、预警登记、撤警登记、观察录入等行政手续，但不代乡派播。
* 每个村在任一时刻只有一个"现任值班乡"：授权窗口内且未交还的村由邻乡负责，
  已交还村或授权到期后回到原乡。派播指令按村逐个校验处置权。
* 整组停（`stop-group`）物理上无法按村切分：只要组内还有村由该值班方负责即可
  受理，但安全闸门会对组内**全部仍有效内容**生效。

## 3. HTTP 接口

所有写接口为 `POST`，body 为 JSON，除 GET 外都要带 `actor`，可选 `at`（ISO-8601，
必须带时区）。错误统一返回 `{"error": 码, "detail": {...}}`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| GET | `/board` | 全县值班看板 |
| GET | `/board/villages/<village>` | 按村查看应播预警、各渠道实际状态、下一步处置方 |
| GET | `/warnings/<id>/withdrawal` | 撤警计划：已确认/待完成村、缺口、推送范围 |
| GET | `/commands/<id>` | 单条指令及其最新观察 |
| GET | `/commands/<id>/observations` | 该指令全部观察（含迟到/stale） |
| GET | `/observations` | 全部设备回报 |
| GET | `/audit` | 审计日志 |
| POST | `/towns/online` | `{town, online}` 上报乡镇在线/离线 |
| POST | `/delegations` | 建授权：限定村、渠道白名单、有效时段 |
| POST | `/delegations/<id>/return` | `{village}` 村交还，原乡立即恢复处置权 |
| POST | `/warnings` | `{id, villages}` 登记预警 |
| POST | `/dispatch` | 派播，见下 |
| POST | `/commands/<id>/reconcile` | 断链后用同 ID 向对端对账 |
| POST | `/commands/<id>/retry` | 同 ID 同内容重发（对端幂等） |
| POST | `/commands/<id>/takeover` | 现任值班乡接管未决处置 |
| POST | `/observations` | 录入独立监听结果 `{remote_id, kind, heard, observed_at}` |
| POST | `/observations/fetch` | 主动向对端拉取监听结果再入账 |
| POST | `/withdrawals` | `{warning, villages}` 登记局部撤警 |

### `/dispatch` 动作

* `start`：`{channel, action:"start", alert, villages?}`。`per_alert` 渠道可带
  `villages` 限定村（替代补播场景）；`whole_group` 渠道按登记 scope 整组处理。
* `stop`：按预警号停止，只影响该预警（要求渠道支持 `per_alert`）。
* `stop-group`：整组停，仅 `whole_group` 渠道。组内仍有效（未撤警）的每条
  （预警, 村）都必须已在某个替代渠道确认成音，否则返回 `409 unsafe_group_stop`，
  `detail.uncovered` 精确列出缺口。

### 指令状态机

```
send_failed        接单前对端不可达，可 retry
sent_unknown       已发出但结果未知（如 commit_disconnect），保留可能影响范围，可 reconcile/retry
rejected           对端明确拒单（含原因）
accepted_pending   已接单，等待独立观察；accepted 不等于听见广播
active             最新有效观察为 audible（实际成音）
stopped            最新有效观察为 silent（实际静默）
```

## 4. 关键规则落地

1. **授权边界**：村、渠道白名单、`[valid_from, valid_until)` 时段三重限定；
   到期不触发任何停机，只是代理不能再下新指令。
2. **局部撤警**：按（预警, 村）生效。未撤警村、同组其他有效预警继续播。
3. **老设备整组停**：先在安全替代渠道把仍有效内容补到 audible，再停整组；
   替代未证实可用时，`unsafe_group_stop` 与撤警计划里的 `blockers` 明确显示
   哪部分无法完成、哪些村仍在推送范围。
4. **迟到回报**：已确认撤警的村不会被旧启动 audible 重新挂回；回报本身保留，
   冲突条目标记 `stale` 并在审计与观察接口可见。整组停后迟到的旧启动回报不会让
   该渠道会话复活（会话按指令流折叠，整组停终结在播会话）。
5. **接管交还**：村交还后原乡可 `takeover` 同一项未决处置；旧代理再派播会被
   `not_current_duty` 拒绝且不会触达设备；旧设备的实际回报照常入账给现任值班员。

## 5. 测试

```sh
# 普通模式
python3 -m unittest discover -t . -s tests -v
python3 -m unittest test_partner_stub -v

# 隔离模式（-I 不读环境变量，需用 -t 指定仓库根以便导入本地包）
python3 -I -m unittest discover -t . -s tests
```

真实进程端到端叙事在两个服务启动后运行：

```sh
python3 tests/smoke_driver.py 8080
```
