# Responses v2：Linux 部署与完整历史采集

本版在原有 Chat Completions 代理上增加 Responses 原生转发、流式重组、持久化父回复索引、完整历史导出。业务工具仍由数字员工执行。

## 1. 新版包含什么

| 接口 | 功能 |
|---|---|
| `POST /v1/responses` | JSON/SSE 转发，文本、普通 function call、并行调用与参数分片 |
| `GET /v1/responses/{id}` | 转发查询或流式恢复（以上游能力为准），记录后台任务最终结果 |
| `DELETE /v1/responses/{id}` | 透传删除操作并保存原始响应；不删除已采集的本地记录 |
| `POST /v1/responses/{id}/cancel` | 透传取消操作并记录结果 |
| `POST /v1/chat/completions` | 保留原来的 JSON/SSE 与 function tools 支持 |

所有这些接口沿用同一个 base_url 和鉴权配置。代理不会将 Responses 请求转换为 Chat Completions 请求；上游必须支持所使用的接口。Responses -> 对话格式的转换仅发生在离线/自动数据集导出阶段。

## 2. Linux 首次部署

代理部署在 `/mnt/data/zht/qwen-collector-proxy`，Conda 环境与采集数据分别放在其 `.conda-env` 和 `data/`；模型权重目录仍为 `/mnt/data/models/Qwen3.8-27B`。

将新版 `qwen-collector-proxy-v2.tar.gz` 上传到服务器，然后执行：

```bash
mkdir -p "/mnt/data/zht"
tar -xzf qwen-collector-proxy-v2.tar.gz -C "/mnt/data/zht"
cd "/mnt/data/zht/qwen-collector-proxy"

conda create -p "$PWD/.conda-env" python=3.12 -y
conda activate "$PWD/.conda-env"
python -m pip install -r requirements.txt

cp collector.env.example collector.env
chmod 600 collector.env
bash start_proxy.sh
```

默认监听 `127.0.0.1:9000`，上游 `http://127.0.0.1:8000/v1`，本地数据 `./data`。跨服务器时在 `collector.env` 设置 `PROXY_HOST=0.0.0.0` 和随机 `PROXY_API_KEY`，框架填写相同 key。启动脚本、Conda、systemd 的完整说明见 [README.md](README.md)。

框架配置：

```text
API 模式：OpenAI Responses
base_url：http://采集代理IP:9000/v1
model：Qwen/Qwen3.8-27B（或你的 served model name）
api_key：PROXY_API_KEY 对应的密钥；默认本机无鉴权时可填 EMPTY
stream：true
store：true（使用 previous_response_id 时）
```

模型请求的 `tools` 使用 Responses 的扁平 function schema：`{"type":"function","name":"...","parameters":{...}}`。框架通常会自行处理这个协议差异。样例见 [example_responses.json](example_responses.json)。

## 3. vLLM 的 previous_response_id 设置

在模型的 Conda 环境和终端中启动，代理使用另一个环境：

```bash
conda activate qwen38
VLLM_ENABLE_RESPONSES_API_STORE=1 CUDA_VISIBLE_DEVICES=0,1 \
vllm serve /mnt/data/models/Qwen3.8-27B \
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

`VLLM_ENABLE_RESPONSES_API_STORE=1` 设置在 **vLLM 进程**，不是代理进程。请求应使用 `store:true`，并从第一轮开始经过采集代理。先用下面的检查脚本确认你安装的 vLLM 版本与 Qwen 工具解析器确实能完成两轮原生 Responses 工具调用。

这里从 `/mnt/data/models/Qwen3.8-27B` 加载本地模型，并用 `--served-model-name Qwen/Qwen3.8-27B` 保持请求中的模型名称一致。

vLLM 官方说明该存储默认关闭，开启后把 input/output 保存在进程内存里，重启丢失且可能持续增长。代理的磁盘索引可以跨代理重启恢复采集历史，但不会替上游恢复已经丢失的 response 状态，也不会重写请求去绕开上游错误。批量生成时应监控 vLLM 主机内存，重启模型服务后从新会话开始。见 [vLLM 环境变量说明](https://docs.vllm.ai/en/latest/configuration/env_vars/)。

## 4. 验证两轮流式 function call

启动 vLLM 与代理后，在项目目录运行：

```bash
conda activate "$PWD/.conda-env"
python smoke_responses.py \
  --base-url http://127.0.0.1:9000/v1 \
  --model Qwen/Qwen3.8-27B
```

开启了代理鉴权时，先在当前 shell 设置 `PROXY_API_KEY` 为相同的密钥。检查脚本会真实调用模型两次：第一轮请求 `get_order_status`；脚本使用明确标记的模拟工具结果回复；第二轮通过 `previous_response_id` 关联第一轮并生成最终回答。它不操作真实订单系统。

也可以单独观察 SSE 原文：

```bash
curl -N http://127.0.0.1:9000/v1/responses \
  -H 'Content-Type: application/json' \
  --data-binary @example_responses.json
```

有鉴权时加 `-H 'Authorization: Bearer 你的代理密钥'`。第一轮应出现 `response.output_item.added`、`response.function_call_arguments.delta` 等事件，最终以 `response.completed` 结束。原始 SSE 保存在 `response.body`，代理不修改事件再转发。

## 5. previous_response_id 如何关联

每轮请求和响应保存在 `data/raw/UTC日期/UUID/`；`data/state/responses.sqlite3` 保存 `response_id -> 创建记录、完整结果记录`。历史以 response ID 链为准，支持同一父回复上的多条分支，不按最近一次请求或相似问题猜测归属。

```text
r1：input=用户问题            -> output=function_call(call_A)
r2：previous=r1, input=工具结果A -> output=function_call(call_B)
r3：previous=r2, input=工具结果B -> output=最终答复
```

r3 的 SFT 样本会自动形成：

```text
prompt：原问题 -> 调用 A -> 结果 A -> 调用 B -> 结果 B
completion：最终答复
```

关联规则：

- 只有当前轮的顶层 `instructions` 参与当前样本；上一轮顶层 instructions 不自动继承。这遵循 OpenAI 的 Responses 语义。直接作为 input message 发送的 system/developer 消息则属于历史输入。
- 函数的 `call_id` 对应训练消息里的 `tool_calls[].id` / `tool_call_id`；原生 item 的 `id` 是另一个标识，不混用。
- 参数 `.delta` 片段累加，`.done` 和完整 terminal output 使用最终值覆盖，避免重复拼接。
- 收到终止事件后，在将该事件转发给框架前发布历史索引，避免框架立即发起下一轮时找不到父回复。
- 代理启动和离线导出时会扫描原始记录修复索引，因此目录排序不要求父记录排在子记录前面。
- 上游 URL 或上游 API key 改变后会使用不同历史作用域，防止不同后端/凭据的 ID 串联；因此跨后端切换或 key 轮换请开始新会话。
- 单条关联链最大 1024 个祖先；循环、缺失父记录或无法读取的父记录都标记为历史不完整。

如果父回复不曾经过代理，代理不能凭一个 ID 还原原问题。它仍透传请求并保存原始记录，标记 `history_unresolved`，不生成声称完整的 SFT/原生样本。以后补齐父记录，可重新执行 export 恢复样本。此时自动日文件不会回溯补写旧请求，正式数据集以重新导出为准。

后台生成：原始 POST 返回 queued/in_progress 时只保存记录；如果框架之后经过代理 GET 查询到 completed，代理使用原创建请求与完成结果导出。重复轮询可能重复追加，正式导出会去重。

## 6. 同时生成两种 JSONL

```text
data/
  raw/日期/请求UUID/
    request.json                 # 原始请求体（GET/DELETE 可能为空）
    response.body                # 原始 JSON 或 SSE
    record.json                  # 协议、状态、重组结果、索引/导出诊断
  state/
    responses.sqlite3            # 持久化索引；有活动写入时可能有 -wal/-shm
  datasets/
    sft-日期.jsonl               # Chat + Responses 通用候选训练集
    responses-native-日期.jsonl  # Responses 完整逻辑历史 input + 原生 output
```

SFT 默认使用 `prompt + completion + tools + metadata`。函数 schema 会从 Responses 格式转成对话训练常用的嵌套 function 格式，参数转换为 JSON 对象；先前模型调用和工具返回放在 prompt，当前回复/调用放在 completion。也可导出 `messages + tools`。

原生数据集保存从已采集祖先展开后的 input、当前 output、当前 instructions/tools、模型和 usage。reasoning、内置工具、文本注释等原生条目保留在这里和原始响应中。SFT 目前支持文本 message、普通 function_call/function_call_output；遇到图像、内置搜索、MCP 原生事件、自定义工具等无法无损转换的条目时，会保留完整原生样本并标记 SFT 转换原因，而非静默丢弃它们。

“完整”指已捕获 response 链的逻辑上下文，不是服务器渲染后的逐 token 输入。服务端截断、压缩或隐藏状态可能改变模型实际可见内容。当前不展开 Conversations API 的服务端会话，也不解析不透明 `item_reference`；这些无法确认完整历史的情况只保留原始记录并标记。建议采集普通 function tools 场景并避免不可见的服务端历史管理。

原始与原生数据保留模型接口返回的可见 reasoning 内容。通用 SFT 默认移除独立 reasoning 字段；`INCLUDE_REASONING=1` 或 export 的 `--include-reasoning` 可保留已返回的文本/摘要。加密 reasoning 原文保存在原生记录，无法当作普通文本还原。9B 模型的 chat template 必须支持对应字段才能参与训练。

元数据包含 `response_id`、`previous_response_id`、`ancestor_response_ids`、`root_response_id` 和 `history_complete`。按 `root_response_id` 或业务任务划分训练/评测集，避免同一链条拆进不同集合；业务是否正确仍需额外校验。

## 7. 重新导出完整训练集

从两种接口共同重建并去重：

```bash
python collector_proxy.py export --data-dir ./data --output ./exports/train.jsonl
```

通用对话格式：

```bash
python collector_proxy.py export --data-dir ./data \
  --output ./exports/messages.jsonl --format messages
```

完整 Responses 原生历史：

```bash
python collector_proxy.py export --data-dir ./data \
  --output ./exports/responses-native.jsonl --format responses-native
```

默认去重，输出已存在时需换文件名或显式 `--overwrite`，并生成 `.stats.json` 统计跳过原因。`responses-native` 格式只包含 Responses 记录，不将 Chat 伪装成原生 Responses。正常导出不会包含 HTTP 错误、中断/不完整响应及缺失祖先的记录。

## 8. 从第一版升级

先停止原代理，保留 `collector.env`、`.conda-env`、`data/`。新版压缩包不包含这些文件，不会覆盖它们：

```bash
systemctl --user stop qwen-collector
tar -xzf qwen-collector-proxy-v2.tar.gz -C "/mnt/data/zht"
cd "/mnt/data/zht/qwen-collector-proxy"
conda activate "$PWD/.conda-env"
python -m pip install -r requirements.txt
systemctl --user restart qwen-collector
journalctl --user -u qwen-collector -f
```

手动启动用户先在原终端 Ctrl+C，再执行解压和 `bash start_proxy.sh`。第一版 Chat 原始数据可继续用新版 export 导出；旧数据没有 Responses 信息，无法补造历史。备份数据时建议先停止采集，再整体备份 raw/ 与 state/；索引损坏可由 raw/ 重建，原始文件应长期保留。

## 9. 验证范围与协议依据

本地模拟测试涵盖普通和流式 Responses、逐字节 SSE、并行调用、三轮工具轨迹、重启、分支、缺失祖先补齐后导出、后台查询、索引重建、跨后端隔离、循环检测和终止事件转发前的索引可见性。没有在你的 Linux/真实 Qwen 环境执行；上线前运行第 4 节的真实两轮检查。

- [OpenAI Responses 函数调用](https://developers.openai.com/api/docs/guides/function-calling)
- [Responses 流式事件](https://developers.openai.com/api/reference/resources/responses/streaming-events)
- [会话状态与 previous_response_id](https://developers.openai.com/api/docs/guides/conversation-state)
- [顶层 instructions 继承规则](https://developers.openai.com/api/docs/guides/migrate-to-responses)
- [vLLM Responses 存储设置](https://docs.vllm.ai/en/latest/configuration/env_vars/)
