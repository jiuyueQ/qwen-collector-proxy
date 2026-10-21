# Qwen 消息兼容处理与 Linux 更新

## 作用与边界

针对 `/v1/chat/completions` 的 `System message must be at the beginning.` 错误，
新增默认开启的 `CHAT_COMPAT_MODE=qwen`。现有 `collector.env` 没有这个变量也会默认开启，
数字员工框架继续使用原来的地址、密钥、模型名称和流式设置。

转换在发送上游前完成，对 JSON 和 SSE 请求使用同一逻辑：

1. 开头连续的 `system` / `developer` 合并为一条 `system`，保留内容顺序及来源标记。
2. 对话中途的 `system` / `developer` 转为带来源标记的 `user` 上下文，保留其在历史中的位置。
3. 原有 `user`、`assistant`、`tool` 及工具 ID、工具参数、工具定义、流式选项保持原值。
4. 已符合要求的请求保持原始字节转发；转换后的请求只重新编码 JSON。

示例：

```text
原始：system → developer → user → assistant → system → user
转发：system(合并开头两条) → user → assistant → user(框架上下文) → user
```

中途指令转换成用户上下文后，其优先级会降低；这不是原始多 system 语义的完全等价实现。
不会把后面才出现的指令移动到历史开头，也不会把工具返回内容提升为系统指令。
原始工具执行顺序不变，但原请求若有缺失 tool_call_id、未配对的工具调用等其他问题，仍需单独处理。

兼容处理支持指令内容为字符串或纯 `type=text` 分片数组。遇到图片、未知分片或额外指令字段时，
整个请求保持原样转发并在 `record.json` 记录 `skip_reason`，由上游报告错误，避免丢失未知内容。
`/v1/responses` 继续原样转发，`previous_response_id` 历史关联逻辑保持原样。

关闭兼容模式：在 `collector.env` 中设置 `CHAT_COMPAT_MODE=off` 并重启代理。
直接运行 Python 时也可以传 `serve --chat-compat off`。

## 原始记录和训练集

每次 Chat 请求保存：

```text
data/raw/<UTC日期>/<request_id>/
  request.json             # 框架原始请求，逐字节保留
  forwarded_request.json   # 实际转发的请求，发给上游前落盘
  response.body            # 原始 JSON 或 SSE 响应
  record.json              # chat_compatibility: 模式、版本、修改位置、原因
```

自动 SFT 和离线重建导出均使用 `forwarded_request.json`，保证训练提示词与教师模型实际输入一致。
样本 metadata 包含转换记录和 `request_source`，可筛选发生兼容转换的样本。
旧版记录没有转发文件时，离线导出继续读取 `request.json`，不会事后改写旧输入。
原始 400 请求保留，但不会因为更新代理就自动补生成训练样本；需要从框架重新发起对应任务。

## 服务器更新与 nohup 重启

先暂停批量任务并等待正在执行的请求结束，再复制整段执行。
代码更新使用 `--ff-only`；若有受 Git 管理的文件被本地改动，Git 会提示处理，不会强制覆盖。
命令复用现有 `.conda-env`、`collector.env`、数据目录和已经运行的模型。

```bash
(
  set -e
  cd /mnt/data/zht/qwen-collector-proxy
  git pull --ff-only origin main
  bash restart_proxy.sh
)
```

本次未增加第三方依赖，不需要重新安装环境。`restart_proxy.sh` 使用 nohup 后台启动，
SSH 断开后继续运行；记录 PID 到 `logs/proxy.pid`，追加日志到 `logs/proxy.log`。
脚本通过 `/proc` 匹配本目录的 `python collector_proxy.py serve` 进程，
也能停止之前未保存 PID 的手动启动实例。使用 SIGTERM 等待最多 45 秒，未退出则中止重启。
脚本有并发重启锁；不会按端口或笼统的 Python 进程名终止其他服务。
此脚本只用于 nohup 部署；不要与 systemd 同时管理同一个代理实例。

查看日志和代理存活状态：

```bash
tail -n 80 /mnt/data/zht/qwen-collector-proxy/logs/proxy.log
curl -fsS http://127.0.0.1:9000/healthz
```

`healthz` 只表示代理存活。恢复数字员工请求后，在日志里出现 `normalized chat request_id=...`
表示请求经过转换，最终是否成功以框架收到的响应和对应 `record.json` 为准。

若原来使用 systemd 用户服务，则改用：

```bash
(
  set -e
  cd /mnt/data/zht/qwen-collector-proxy
  systemctl --user stop qwen-collector
  git pull --ff-only origin main
  systemctl --user start qwen-collector
)
```

本次更新未在你的 Linux 服务器和真实 Agent 请求上执行验证。现有测试没有运行，也没有新增测试。
