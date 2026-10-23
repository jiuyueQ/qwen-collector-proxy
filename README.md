# Qwen 数字员工采集代理 v2.5：Linux 部署方案

把代理放在数字员工框架与 vLLM 之间。数字员工继续执行工具；代理记录每次模型请求与回复，自动生成候选 SFT 数据。

本版同时支持 Chat Completions 和 Responses。Responses 的完整部署、两轮工具调用检查、历史关联规则及旧版升级步骤见 [RESPONSES.md](RESPONSES.md)。

新增 Qwen Chat 消息兼容：默认处理多个或中途出现的 `system/developer`，保留框架原始请求并另外保存实际转发请求。兼容规则、语义影响及服务器 nohup 更新重启命令见 [CHAT_COMPAT.md](CHAT_COMPAT.md)。

新增 Wing 数字员工批量提问脚本：自动识别运行页的普通/AG-UI 接口，支持题目 JSONL、多轮题组、独立会话、断点续跑及平台流式记录。见 [BATCH_QUESTIONS.md](BATCH_QUESTIONS.md)。模型工具调用训练集仍由本采集代理导出。

Windows 用户可双击 `setup_windows.cmd` 安装、`start_windows.cmd` 启动。已附 1000 组中文自然语言题库（1405 次用户发言），默认单并发、会话隔离、失败停止，使用方法见 [WINDOWS_QUICKSTART.md](WINDOWS_QUICKSTART.md)。

Windows 默认已改为**网页自动操作**：在可见浏览器中逐字输入、点击发送、等待页面回复，每组重新打开页面生成新会话。账号由你在浏览器里手动登录，不使用提问接口直连模式。

**按Agent接口逐会话采集**：双击 `setup_agent_api_windows.cmd` 安装、`start_agent_api_windows.cmd` 运行。每个会话完整结束后再执行下一个，同会话保留多轮记忆。详见 [AGENT_API_WINDOWS.md](AGENT_API_WINDOWS.md)。

```text
批量提问脚本 -> 数字员工对话入口
                      |
                      v
           采集代理 :9000/v1
                      |
                      v
          vLLM / Qwen 27B :8000/v1

数字员工收到 tool_calls -> 执行原有工具 -> 将工具结果放入下一次 messages
                                                   |
                                                   v
                                        代理自动采集这部分上下文
```

## 1. 支持范围与数据含义

- `POST /v1/chat/completions`：普通 JSON 与 SSE 流式回复；默认进行 Qwen 消息角色兼容，`CHAT_COMPAT_MODE=off` 可原样转发。支持工具参数分片、并行工具调用和多个 choice。
- `POST /v1/responses`：普通 JSON 与原生 Responses SSE，支持 function call 及 `previous_response_id` 的持久化历史关联。
- `GET/DELETE /v1/responses/{response_id}`、`POST /v1/responses/{response_id}/cancel`：透传并保存生命周期记录；后台生成完成后通过 GET 观察到的完整结果也可关联原创建请求导出。
- `GET /v1/models`：转发模型列表，兼容框架的模型探测。
- `GET /healthz`：代理存活检查，**不代表模型就绪或磁盘有剩余空间**。
- 框架可选择 OpenAI **Chat Completions** 或 **Responses** 模式。当前不实现 Anthropic Messages、音频流、WebSocket 或服务端 Conversations API 的历史展开。
- Responses 的 MCP/内置工具等原生条目会保存在原始记录和完整原生数据集中；通用 SFT 转换目前支持文本和普通 function tools，不会悄悄丢弃不支持的条目。
- Chat Completions 支持现代 `tool_calls` 格式；它的旧式 `function_call` 仍透传并保留原始记录，暂不自动转换。Responses 原生 `type=function_call` 条目受支持。
- 工具实际执行发生在框架。只有框架把工具结果发给模型，代理才能看到它；框架内部日志、隐藏工具、任务最终业务状态无法从模型链路直接获知。
- 脚本不主动提问。接入后，人工对话和批量请求数字员工入口都会自动被采集。每道批量题建议在框架创建新会话，避免上下文污染。

原始记录保存独立的 `reasoning_content` / `reasoning` 字段；自动训练集默认去掉这些独立字段，但保留普通 `content` 中的决策说明。脚本不会修改正文中的 `<think>` 文本。`INCLUDE_REASONING=1` 可以让自动数据集保留独立推理字段，后续训练时需要目标 9B 模型的模板明确支持这些字段，否则可能被忽略。模型接口没有返回的内部信息无法由代理获得。

## 2. 推荐部署拓扑

本项目部署根目录为 `/mnt/data/zht`，模型权重仍使用 `/mnt/data/models/Qwen3.8-27B`：

| 内容 | 服务器路径 |
|---|---|
| 代理项目 | `/mnt/data/zht/qwen-collector-proxy` |
| 代理 Conda 环境 | `/mnt/data/zht/qwen-collector-proxy/.conda-env` |
| 原始记录、数据集、历史索引 | `/mnt/data/zht/qwen-collector-proxy/data` |
| 模型权重 | `/mnt/data/models/Qwen3.8-27B` |

先把 vLLM 和代理部署在同一 Linux 主机：vLLM 监听 `127.0.0.1:8000`，代理监听 `127.0.0.1:9000`。代理使用 CPU 和磁盘，不额外加载模型，不占用 L20 显存。

框架在别的主机时，将代理改为 `0.0.0.0:9000`，设置 `PROXY_API_KEY`，让框架配置相同的 key。只开放代理端口给框架所在网络。若框架通过 Docker 运行，容器中的 `127.0.0.1` 是容器本身，base_url 应填写容器可达的代理地址。

## 3. 上传与创建 Conda 环境

将压缩包上传到服务器，解压到固定目录。tar.gz 和 zip 二选一：

```bash
mkdir -p "/mnt/data/zht"
tar -xzf qwen-collector-proxy-v2.tar.gz -C "/mnt/data/zht"
# 或：unzip qwen-collector-proxy-v2.zip -d "/mnt/data/zht"
cd "/mnt/data/zht/qwen-collector-proxy"

conda create -p "$PWD/.conda-env" python=3.12 -y
conda activate "$PWD/.conda-env"
python -m pip install -U pip
python -m pip install -r requirements.txt

cp collector.env.example collector.env
chmod 600 collector.env
```

独立代理环境不需要安装 torch、CUDA 或 vLLM。`requirements-tested.txt` 是开发环境实际验证过的直接依赖版本，可替代 `requirements.txt` 做复现安装；本包未在你的 Linux 主机和真实模型上验证。

## 4. 启动教师模型（已经运行则跳过）

在另一个终端使用你已有的 Qwen / vLLM 环境：

```bash
conda activate qwen38
VLLM_ENABLE_RESPONSES_API_STORE=1 CUDA_VISIBLE_DEVICES=0,1 vllm serve /mnt/data/models/Qwen3.8-27B \
  --served-model-name Qwen/Qwen3.8-27B \
  --tensor-parallel-size 2 \
  --dtype bfloat16 \
  --max-model-len 32768 \
  --gpu-memory-utilization 0.90 \
  --reasoning-parser qwen3 \
  --enable-auto-tool-choice \
  --tool-call-parser qwen3_coder \
  --host 127.0.0.1 \
  --port 8000
```

模型服务本身需要兼容版本的 vLLM、Transformers、NVIDIA 驱动。以已启动成功的模型服务为准；本代理不替换、安装或修改模型运行环境。数字员工系统提示词与工具 schema 也计入 32K 上下文预算。

上面的 `vllm serve` 使用已下载的本地权重目录，`--served-model-name` 保持 API 模型名为 `Qwen/Qwen3.8-27B`，框架和检查脚本继续使用这个名称。

如果使用 Responses 的 `previous_response_id`，上游需启用存储且创建请求使用 `store:true`。上面的环境变量用于 vLLM 的 Responses 存储。vLLM 官方说明该存储保存在进程内存中、重启丢失，长时间采集需关注内存增长；代理磁盘历史仍保留，但不会自动恢复上游服务器的会话状态。参见 [vLLM 环境变量](https://docs.vllm.ai/en/latest/configuration/env_vars/) 和 [RESPONSES.md](RESPONSES.md)。

## 5. 启动代理

```bash
cd "/mnt/data/zht/qwen-collector-proxy"
bash start_proxy.sh
```

默认：上游 `http://127.0.0.1:8000/v1`，代理 `127.0.0.1:9000`，数据目录 `./data`。

也可不使用启动脚本，直接运行：

```bash
./.conda-env/bin/python collector_proxy.py serve \
  --upstream http://127.0.0.1:8000/v1 \
  --host 127.0.0.1 \
  --port 9000 \
  --data-dir ./data \
  --read-timeout 600
```

`--read-timeout` 是上游连续无数据的等待上限，长推理模型可适当增大。代理不会自动重试生成请求，避免重复生成。保持单个服务实例即可支持多请求并发；自动 JSONL 写入有跨进程文件锁。

### 框架与代理不在同一主机

编辑 `collector.env`：

```dotenv
UPSTREAM_BASE_URL=http://127.0.0.1:8000/v1
PROXY_HOST=0.0.0.0
PROXY_PORT=9000
PROXY_API_KEY=换成你生成的随机密钥
UPSTREAM_API_KEY=
```

可以在服务器执行 `python -c 'import secrets; print(secrets.token_urlsafe(32))'` 生成密钥。`UPSTREAM_API_KEY` 是模型服务自己的 key，若 vLLM 未开启鉴权则留空。代理 key 不会转发给模型，鉴权头不写入原始记录。`collector.env` 是受信任的本地配置，启动脚本会读取它。

框架设置：

```text
API 类型：OpenAI Chat Completions 或 Responses（与框架实际模式一致）
base_url：http://代理服务器IP:9000/v1
api_key：与 PROXY_API_KEY 一致
model：Qwen/Qwen3.8-27B（必须与上游实际 served model name 一致）
stream：继续使用框架原有设置
```

框架若允许配置自定义请求头，可添加 `X-Session-Id` / `X-Task-Id` 便于分组和后续划分数据集。没有这些头也能采集，代理不会猜测会话关联。

## 6. 连通性与流式 function call 检查

默认本机免鉴权配置：

```bash
curl -sS http://127.0.0.1:9000/healthz
curl -sS http://127.0.0.1:9000/v1/models
curl -N http://127.0.0.1:9000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  --data-binary @example_chat.json
```

如果配置了代理 key，每次访问 `/v1/models` 和 `/v1/chat/completions` 均加 `-H 'Authorization: Bearer 你的代理密钥'`。`example_chat.json` 只展示函数调用生成，不执行订单工具；真实工具结果由数字员工框架在下一轮请求中带回。

然后从数字员工实际发一道需要调用工具的问题。观察 `data/raw/` 下有多条模型调用记录，并检查后续请求的 `messages` 里出现 `tool` 角色。先跑少量问题核对这些字段，再批量提问；代理本身不需要额外启动采集任务。

如前置 Nginx，代理路径需要关闭 `proxy_buffering`、开启 HTTP/1.1 并设置足够长的 `proxy_read_timeout`。代理返回 `X-Accel-Buffering: no`，但仍需检查你的网关是否强制缓存。推荐先直连代理确认流式效果。

## 7. 自动保存的文件

```text
data/
  raw/
    2026-10-20/                 # UTC 日期
      <request_uuid>/
        request.json           # 框架原始请求体，字节保留
        forwarded_request.json # Chat 实际转发请求；训练集使用这个输入
        response.body          # 原始响应体；流式时为 SSE（HTTP 内容解码后）
        record.json            # 状态、重组回复、usage、跳过原因等
  datasets/
    sft-2026-10-20.jsonl        # 自动追加的候选训练集
    responses-native-2026-10-20.jsonl # Responses 完整逻辑历史 + 原生输出条目
    sft-2026-10-20.jsonl.lock   # 跨进程文件锁
  state/
    responses.sqlite3         # Responses response_id -> 原始记录索引（持久化）
```

请求 ID 也会通过 `X-Collector-Request-Id` 返回。每条原始记录独立存储，避免不同请求互相覆盖。并发写入 JSONL 会加锁；每个请求完成时刷新到磁盘。磁盘故障会影响采集和转发；硬断电或 `kill -9` 仍可能留下未完成记录，应监控磁盘余量并备份原始目录。

`record.json` 中：`state=complete` 仅表示上游响应已完整读取；`candidate_count` 是通过技术过滤的样本数；`skip_reasons` 解释未导出的原因。HTTP 错误或中断保留原始记录；`business_verified=false` 明确表示尚未验证业务正确性。客户端断开后代理关闭上游连接并保存已有内容，不继续后台跑完整个任务。

鉴权头不记录；正文中的业务信息仍会原样保存。Linux 入口使用 `umask 0077` 创建文件，建议数据目录使用独立磁盘和受限账户。

## 8. 导出与训练格式

默认自动生成的是 conversational prompt/completion JSONL：

```json
{"prompt":[{"role":"user","content":"查询订单 A123"}],"completion":[{"role":"assistant","content":null,"tool_calls":[{"id":"call_1","type":"function","function":{"name":"get_order_status","arguments":{"order_id":"A123"}}}]}],"tools":[{"type":"function","function":{"name":"get_order_status","parameters":{"type":"object","properties":{"order_id":{"type":"string"}},"required":["order_id"]}}}],"metadata":{"request_id":"...","business_verified":false}}
```

每次模型请求生成一条样本；`n>1` 时，每个完成的 choice 各一条。原始 tool arguments 保留字符串，训练数据转换为 JSON 对象，方便使用目标模型的工具模板。默认去掉独立推理字段。旧的 assistant 回复和工具结果放在 `prompt` 中，仅本次回复放在 `completion` 中，便于训练器只对本次答案计算 loss，减少多步历史重复监督。

自动过滤包括：响应/传输中断、HTTP/SSE 错误、JSON 解析错误、`finish_reason=length/content_filter/空`、空目标、无效参数 JSON、未知工具名、目标参数不符合工具 JSON Schema。业务正确性、工具真正执行成功与最终任务成功需要另行校验，不能仅凭 schema 合法判定。

正式训练前从原始记录重建去重版本：

```bash
./.conda-env/bin/python collector_proxy.py export \
  --data-dir ./data \
  --output ./exports/train.jsonl
```

默认去重，以完整训练内容为准，不包含 metadata。不会覆盖已有输出，除非显式加 `--overwrite`。附带生成 `train.jsonl.stats.json` 统计表。自动日文件保留所有有效请求，客户端重试可能产生重复，正式训练建议使用重建导出文件。采集运行中也能导出，但仍在进行的记录会被跳过；需要完整快照时先停止批量任务并等待请求结束。

需要常见的 `messages + tools` 格式：

```bash
./.conda-env/bin/python collector_proxy.py export \
  --data-dir ./data \
  --output ./exports/messages.jsonl \
  --format messages
```

需要独立推理字段时加 `--include-reasoning`。目标 9B 的 tokenizer/chat template 必须正确处理工具格式；如果模板会忽略独立 reasoning 字段，不能仅靠数据中存在该字段就认为参与训练。`metadata` 用于审计，训练时忽略。先按任务/会话/问题模板划分训练与评测，再处理改写和扩增，避免同一任务的相邻步骤跨集合泄漏。

只重建指定日期的候选数据，可以重复传入 `--date`：

```bash
./.conda-env/bin/python collector_proxy.py export \
  --data-dir ./data \
  --date 2026-10-22 --date 2026-10-23 \
  --output ./exports/teacher-20261022-23-messages.jsonl \
  --format messages
```

日期匹配 `data/raw/YYYY-MM-DD/`，以请求开始的 UTC 日期为准，与自动生成的 `sft-YYYY-MM-DD.jsonl` 使用的日期一致；不是北京时间自然日。
不传 `--date` 时仍导出全部日期。指定目录不存在时直接报错，避免静默漏导。
Responses 的 `previous_response_id` 仍可读取其他日期的历史作为上下文，但其他日期的记录不会单独成为本次导出目标。
统计文件的 `selection` 会记录所选日期和日期依据。更新离线导出脚本无需重启正在运行的采集器。

TRL 的 conversational prompt/completion 与 tools 格式参考：
- https://huggingface.co/docs/trl/en/dataset_formats
- https://huggingface.co/docs/trl/en/sft_trainer

## 9. systemd 常驻部署（用户服务）

以下命令假设项目位于 `/mnt/data/zht/qwen-collector-proxy`，环境位于该目录的 `.conda-env`，配置已写入 `collector.env`：

```bash
mkdir -p "$HOME/.config/systemd/user"
cp qwen-collector.service "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now qwen-collector
systemctl --user status qwen-collector
journalctl --user -u qwen-collector -f
```

需要退出 SSH 后持续运行并在开机时启动，可由有权限的账户开启：

```bash
sudo loginctl enable-linger "$USER"
```

修改配置后执行 `systemctl --user restart qwen-collector`。停止：`systemctl --user stop qwen-collector`。自定义安装目录时修改 service 中的 `WorkingDirectory` 和 `ExecStart`。vLLM 是独立服务，代理不会自动启动它。

## 10. 测试与已知边界

```bash
python -m pip install pytest
python -m pytest -q test_proxy.py test_responses.py
```

测试使用本地模拟上游，无 GPU/网络依赖，覆盖 UTF-8 与 SSE 分片、并行函数参数重组、上下文工具结果、鉴权、原文转发、失败记录、断流、客户端断开及重导出去重。尚需在你的 Linux 主机完成真实模型与数字员工的端到端检查。

代理保留框架输入和模型输出的原始内容，不执行工具、不修改提示词、不自动生成批量问题、不推断任务成功。若框架会压缩历史，记录保存的是模型当时真正看到的压缩上下文；它无法还原框架未发送的内容。重导出可从完整原始记录恢复自动追加失败的样本；进程崩溃留下的 `running/incomplete` 记录不会被当作完整样本导出。

实现依据：
- HTTPX 异步流式转发与连接关闭：https://www.python-httpx.org/async/
- FastAPI StreamingResponse：https://fastapi.tiangolo.com/advanced/custom-response/
- vLLM OpenAI 兼容接口：https://docs.vllm.ai/en/latest/serving/openai_compatible_server/
- Qwen vLLM 配方：https://recipes.vllm.ai/Qwen/Qwen3.8-27B
