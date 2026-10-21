# Windows：按会话串行调用Agent接口

此入口直接调用数字员工平台的Agent接口，经过原有系统提示词、工具定义、工具执行及模型配置。
本版在控制台运行，不需要打开网页。已附1000组通用任务题库，是否适合实际业务仍需审阅。

## 1. 双击运行

1. 解压到固定目录，例如 `D:\qwen-agent-api`。
2. 首次使用双击 `setup_agent_api_windows.cmd`，安装独立环境，仅需要httpx和filelock。
3. 双击 `start_agent_api_windows.cmd`。
4. 选择运行页地址、题库、会话数量、重复次数及保存目录。
5. 输入平台租户编码、用户名和密码。密码隐藏输入，不写入文件。

需要Python 3.10或更高版本，Windows电脑必须能访问该平台地址。无需CUDA、PyTorch或浏览器组件。
运行页地址用于确定平台地址和agentConfigId；脚本不会操作网页。
`agent_api.local.json`只保存地址、租户编码及用户名，被Git忽略，不保存密码或Token。

## 2. 一行题目 = 一个会话

```jsonl
{"id":"q001","question":"读取当前页面的标题和查询条件。"}
{"id":"q002","question":"打开用户管理，查询用户名为admin的记录。"}
{"id":"q003","turns":["在测试表单中填写名称“采集演示001”，先不要提交。","读取刚才填入的值，仍然不要提交。"]}
```

执行顺序：

```text
会话A：q001 → 等待Agent终止事件 → 保存 → 等3秒
会话B：q002 → 等待Agent终止事件 → 保存 → 等3秒
会话C：q003第1轮 → 等待结束 → 等1秒 → 第2轮 → 等待结束 → 保存
```

- 每组及每次重复采样使用新的UUID；不会把上一组对话拼进下一组请求。
- 同组多轮共用session_id/threadId，沿用平台会话内记忆。启动器显式使用 `--allow-agent-memory`。
- 平台后端也需要按session/thread分区保存记忆；脚本不能隔离平台内部的用户级或全局记忆。
- Agent执行工具期间，脚本持续接收SSE事件，不另外发起下一轮或下一会话。
- 使用自动模式：AG-UI开启且有agent_code时调用网关agui/run，否则调用debug-chat/stream。
- 单进程单并发，且相同输出目录、本机相同平台/租户/智能体有文件锁。

## 3. PowerShell直接运行

在项目目录执行，用你的实际运行页地址替换下面的占位地址：

```powershell
.\.agent-api-env\Scripts\python.exe -u batch_agent_questions.py --page-url 'http://平台IP:5177/agent-runtime/智能体配置ID' --input question_banks/readonly.jsonl --output data/agent-api/run-001 --login --allow-agent-memory --interval 3 --turn-interval 1
```

可将 `--input` 改成自己的JSONL文件，或 `question_banks/all_1000.jsonl`。
全部题库含200组保存/提交任务，应使用业务测试环境；工具与字段是否可用要以真实框架为准。
`--limit 10` 本次最多执行10个尚未处理的会话，`--repeat 2` 每组采样两次，每次新建会话。

## 4. 负载和异常

默认最多3轮/会话、每轮问题4000字符，检查平台生成令牌数量不超过4096。
每轮读取超时600秒、总时限1800秒，可通过 `--read-timeout` / `--task-timeout` 调整。

- 未收到成功终止事件不会当作完成。
- 工具返回结构化失败时，不继续该会话后面的固定追问。
- OOM、HTTP429/503、超时/断线未确认结束、前端工具中断需要人工处理时停止整批。
- 连续失败3组时停止；不会自动重发。
- `needs_action` 表示任务依赖前端工具处理，本版不伪造结果，也不自动resume。

显存是否足够还取决于模型服务、其他GPU进程及工具返回长度。vLLM保持 `--max-num-seqs 1`，
避免另一个客户端同时发起批量任务。脚本无法保证任意情况下都不发生OOM。

## 5. 结果与续跑

```text
data/agent-api/<批次>/
  qa.jsonl
  cases/<题目指纹>/case.json
  cases/.../attempts/<尝试ID>/turn-001/
    request.json       # 平台请求体，userKey已脱敏，不保存Authorization头
    response.body      # 已收到的原始SSE
    events.jsonl       # 解析事件，包括工具事件
    result.json        # 完成状态、回复、返回的reasoning、工具事件、session_id
```

每组实际会话ID保存在case.json中。相同题库和输出目录再次运行时跳过已有记录，
包括未成功的题组；剩余未处理题组继续使用新会话执行。
显式加 `--retry-failed` 才会重做未成功题组，可能重复业务工具操作，旧尝试仍保留。

本地qa.jsonl用于查看最终问答，不包含完整模型工具轨迹。
用于9B的function call训练集继续由模型服务器上的采集代理生成：

```text
/mnt/data/zht/qwen-collector-proxy/data/datasets/sft-YYYY-MM-DD.jsonl
```

本次没有新增或运行测试，没有登录平台或执行实际提问。接口适配依据此前只读分析的前端源码。
