# 山区应急广播的跨乡代理与分区撤警

本地演练资料和对端协议桩使用合成编号。dispatch 的 accepted 仅为设备接单，不代表听见广播。device-observation 是独立监听结果。整组停机的影响范围为设备登记范围。

`fixtures/registers.json` 保存模拟业务台账。每个喇叭组的 scope 列明受物理组命令影响的村，渠道可用时间来自值班登记，实际命令接受情况由对端提供。

`fixtures/partner-exchanges.json` 给出合成对端调用的请求和回报示例。每个条目独立，文件顺序没有业务含义。请求正文按匹配的 `operation` 和 `request` 选择示例。对端只模拟设备或合作方的应答，不处理业务服务的规则。

使用 Python 3.11.2 和标准库，无需安装依赖。运行以下命令核验台账与对端联调协议。

```sh
python3 -m unittest -v
python3 partner_stub.py --port 8765
```

对端只监听本机。向 `POST /operations` 提交 JSON 对象，含字符串 `id`、字符串 `operation` 和对象 `request`。首次接单记入对端日志，正常返回 200，格式为 `{"id": "...", "reply": {...}}`。条目配置为 `commit_disconnect` 时，接单会写入对端日志，但首次 HTTP 连接没有响应。按该 ID 调用 `GET /operations/<id>` 查询回报，未接单返回 404。同一个 ID 和内容的重试返回原回报，复用 ID 提交其他内容返回 409。无对应条目的请求返回 422。可通过 `--exchanges` 传入同格式的其他联调资料。

重启业务服务进行联调时可保持对端进程运行。若也需要重启桩，可通过 `--journal` 指定临时 SQLite 文件保留对端接单记录。
