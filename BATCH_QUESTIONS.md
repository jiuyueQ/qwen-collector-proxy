# 从 Wing 数字员工运行页批量提问

`batch_agent_questions.py` 调用数字员工运行页实际使用的接口。页面不必一直开着。
它不修改框架代码，通过配置中的教师模型完成任务；模型链路中的采集代理继续保存模型输入、输出和工具调用训练样本。

```text
问题 JSONL → 批量脚本 → Wing 数字员工运行接口 → 采集代理 :9000 → vLLM :8000
                              ↓                  ↓
                        工具执行和结果回传      工具调用 SFT JSONL
```

## 1. 接口与运行模式

根据用户提供页面可读的前端代码（2026-10-21）实现：

- 先 GET `/api/admin/agent-configs/{agentConfigId}`，自动识别 `agui_enabled` 和 `agent_code`。
- 普通模式 POST `/api/admin/agent-configs/{id}/debug-chat/stream`，请求为 `message`、`session_id`。
- AG-UI 模式 POST `/api/gateway/{agent_code}/agui/run`，使用与页面一致的 `threadId/runId/messages/userKey/tenantId/userId`。
- 认证使用平台 `Authorization: Bearer ...` 与 `X-Tenant-ID`。
- 支持 JSON SSE 事件、增量文本、返回的 reasoning、工具事件及终止事件；只收到 HTTP 200 不会被判定为完成。
- 每次调用都是数字员工真实任务，原配置中的工具会照常执行。题目应使用你允许该数字员工执行的业务操作。

默认控制面与网关都与运行页同源，与当前页面前端配置一致。若部署后地址不同，使用
`--control-base https://控制面地址`、`--gateway-base https://网关地址` 指定。
平台令牌只发送给指定的平台接口。它与采集代理的 `PROXY_API_KEY` 是不同凭据。
脚本未在用户平台上登录或执行真实提问，也没有新增或运行测试。

## 2. 准备题目

更新项目后，从示例复制：

```bash
cd /mnt/data/zht/qwen-collector-proxy
git pull --ff-only origin main
cp -n batch_questions.example.jsonl questions.jsonl
```

编辑 `questions.jsonl`：每行一个 JSON，不要包在一个大数组中。单题使用 `question`，
同一会话的多轮问题使用 `turns`。脚本等待上一轮完整结束后再发送下一轮。

```jsonl
{"id":"q001","question":"填写第一道实际业务问题"}
{"id":"q002","question":"填写第二道需要工具协助的业务问题"}
{"id":"q003","turns":["第一轮问题","第二轮补充信息","第三轮追问"]}
```

`id` 建议保持稳定且唯一。不同题组使用不同会话；同一题组的多轮共用会话。
如果 Agent 启用了跨会话长期记忆，仅新建 session/thread 不能保证完全隔离长期记忆。

运行位置必须能够访问 Wing 的内网地址；模型服务器若不通该内网，可在能访问页面的 Windows 电脑运行。
Python 环境需要 `httpx` 和 `filelock`，现有采集代理环境已包含这些依赖。

## 3. Linux：平台账号交互登录

使用平台登录页的租户编码、用户名和密码。密码会隐藏输入，不写入输出文件。
登录返回的 token、tenant ID、user ID 由脚本在内存中使用，不需要改数字员工配置。

```bash
cd /mnt/data/zht/qwen-collector-proxy
./.conda-env/bin/python batch_agent_questions.py \
  --page-url 'http://平台IP:5177/agent-runtime/智能体配置ID' \
  --input questions.jsonl \
  --output data/batch/run-001 \
  --login
```

`--limit 1` 可限制本次只发一组题；`--repeat 3` 为每组题采样三次，每次新建会话。
默认单并发、题组间隔 1 秒，适用于当前两张 L20、`max-num-seqs=1` 的模型服务。
每轮读取超时默认 600 秒，总时限默认 1800 秒，可通过 `--read-timeout` / `--task-timeout` 调整。

## 4. Linux：已有平台 Token，nohup 后台运行

在浏览器 F12 → Network 中查看该平台一次请求的 Request Headers：
`Authorization: Bearer ...` 后面的内容为平台 Token，`X-Tenant-ID` 是租户 ID。
不要把采集代理 key 填在这里，也不要把令牌提交到 GitHub。

```bash
cd /mnt/data/zht/qwen-collector-proxy
read -r -s -p '平台登录 Token（不含 Bearer 前缀）: ' WING_TOKEN
printf '\n'
export WING_TOKEN
read -r -p '平台 X-Tenant-ID: ' WING_TENANT_ID
export WING_TENANT_ID
# AG-UI 模式可额外设置平台用户 ID；与页面一致，不提供时发送空字符串。
# export WING_USER_ID='平台用户ID'
mkdir -p logs
nohup ./.conda-env/bin/python -u batch_agent_questions.py \
  --page-url 'http://平台IP:5177/agent-runtime/智能体配置ID' \
  --input questions.jsonl \
  --output data/batch/run-001 \
  > logs/batch-questions.log 2>&1 < /dev/null &
echo $! > logs/batch-questions.pid
unset WING_TOKEN
```

查看进度：`tail -f logs/batch-questions.log`。
JWT 即将到期时，在下一轮请求前通过平台 `/api/auth/refresh` 刷新内存中的令牌。
若平台拒绝认证或权限不足（401/403），停止整批；重新登录后用相同命令和输出目录继续。
不解析的非 JWT token 按原值使用，到期需要重新提供。

## 5. Windows PowerShell

可以将项目下载到电脑，在项目目录执行。以下采用 Windows 自己的 Python，
无需安装 vLLM、PyTorch 或 CUDA：

```powershell
python -m pip install httpx filelock -i https://pypi.tuna.tsinghua.edu.cn/simple
Copy-Item batch_questions.example.jsonl questions.jsonl
python batch_agent_questions.py --page-url 'http://平台IP:5177/agent-runtime/智能体配置ID' --input questions.jsonl --output data/batch/run-001 --login
```

Windows 上运行时保持该脚本进程运行。无论脚本在 Windows 还是 Linux，
工具 SFT 仍由模型服务器上已部署的采集代理保存。

## 6. 数据保存与断点续跑

```text
data/batch/run-001/
  qa.jsonl                      # 完成题组的最终问答；每批退出时从记录重建
  cases/<题目指纹>/
    case.json                   # 题目、会话 ID、状态、当前尝试、各轮结果
    attempts/<尝试ID>/turn-001/
      request.json              # 平台调用请求，AG-UI userKey 已脱敏
      response.body             # 收到的原始 SSE 字节，保存至终止事件或断开
      events.jsonl              # 解析后的全部已收到事件
      result.json               # 重组文本、reasoning、工具事件、HTTP 状态等
```

不保存 Authorization 头、登录密码或登录/刷新响应；AG-UI 请求体的 `userKey` 会替换为 `[REDACTED]`。
业务正文和平台返回的事件保存在本地数据目录。题目、输出、日志和环境文件默认被 Git 忽略，
只有无凭据的示例题目文件入库。

同一输出目录有文件锁，防止两份脚本同时跑同一批。再次运行相同命令时：

- 成功题组直接跳过。
- 失败、超时、断线或运行中断的题组也默认跳过，因为工具可能已经执行。
- 显式加 `--retry-failed` 才会以新会话重新执行整组，旧尝试记录保留；可能重复工具操作。
- 需要新一轮实验或修改了平台模型/系统提示词时，换一个 `--output` 目录。

AG-UI 的 `RUN_FINISHED outcome=interrupt` 标记为 `needs_action`，不计为完整问答，
脚本不伪造前端工具结果、不自动调用 resume。页面中手动添加的前端调试工具也不会自动带入脚本。
需要前端交互的业务先保留这些待处理记录，本版主要用于框架服务端能够自主完成的任务。
服务端工具事件会原样记录；一次运行完成不代表工具或业务结果已经人工确认正确。

## 7. 哪份数据用来微调 function call

`data/batch/.../qa.jsonl` 是方便核查的最终问答，**不包含完整模型工具调用上下文**。
用于 function call 微调的数据仍是模型服务器上：

```text
/mnt/data/zht/qwen-collector-proxy/data/datasets/sft-YYYY-MM-DD.jsonl
```

确保平台中这个数字员工的模型配置指向采集代理，所有相关子 Agent 的模型调用也经过代理。
批量脚本提交到平台的 session_id/threadId 不保证被平台转发成代理的 X-Session-ID；
两边均有自己的原始记录，但本脚本不声称能自动一一关联所有代理请求。

正式训练集在模型服务器上从原始记录重建并去重：

```bash
cd /mnt/data/zht/qwen-collector-proxy
./.conda-env/bin/python collector_proxy.py export \
  --data-dir ./data --output ./exports/train.jsonl
```

人工核查业务正确性和工具结果后，再用于目标 9B 模型训练。
