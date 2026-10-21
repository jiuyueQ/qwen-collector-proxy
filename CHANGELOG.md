# v2.5

- 新增Windows Agent接口专用安装和启动入口，无需浏览器依赖。
- 一行题目对应一个独立会话，同组多轮串行，保留平台会话内记忆。
- 会话间默认3秒、同会话轮间默认1秒，新增逐轮进度输出，保留已有异常停止和断点记录。
- 新增AGENT_API_WINDOWS.md；未运行测试或实际平台提问。

# v2.4

- 新增 batch_browser_questions.py，在可见浏览器中逐字输入、点击发送并等待页面回复。
- Windows 默认改为网页模式，手动浏览器登录，独立浏览器配置保存登录状态。
- 每组重新打开运行页，由页面创建会话ID；被动观察并检查会话ID，不直接发起提问接口请求。
- 保存页面回复、工具卡片、会话ID与截图，服务器采集器继续保存完整模型轨迹。
- 原API模式保留；未运行安装、浏览器自动提问或测试。

# v2.3

- 新增 Windows 独立安装与启动入口，只安装批量脚本的 httpx/filelock 依赖。
- 新增 1000 组中文自然语言题库、1405 次用户发言，覆盖查询、表单、提交确认、日期、重复/缺参与搜索。
- 默认只读题库，保存/提交题单列；框架在运行时提供工具 schema，不在题目中编造函数参数或工具结果。
- 批量记录保留题目分类；观测到结构化工具失败后停止该题组的后续追问。
- 默认单并发、3 秒间隔、每组最多 3 轮、单轮问题不超过 4000 字符；检查平台输出上限不高于 4096。
- 同机同智能体互斥锁；默认要求关闭平台记忆模块，UUID 会话按题组隔离。
- Windows 启动入口可选择保留会话内记忆，默认该选项；不同题组仍分配不同 UUID，跨会话记忆分区依赖平台后端。
- 观测 OOM、429/503、超时/断流未确认结束、前端中断或连续失败时停止整批，不自动重试。
- 新增 WINDOWS_QUICKSTART.md；未运行测试或平台任务。

# v2.2

- 新增 batch_agent_questions.py，按 Wing 运行页实际接口自动选择 debug-chat SSE 或 AG-UI SSE。
- 支持 JSONL 题目、多轮题组、重复采样、逐题独立会话、顺序执行和显式失败重跑。
- 支持平台账号交互登录、环境变量令牌和 JWT 到期前刷新；不保存凭据。
- 保存流式原始响应、解析事件、工具事件和断点状态，另导出最终问答 QA；function call SFT 继续由代理生成。
- 新增 BATCH_QUESTIONS.md 和无凭据题目示例；未运行测试或真实平台提问。

# v2.1

- 新增默认开启的 Qwen Chat system/developer 消息兼容，可通过 CHAT_COMPAT_MODE=off 关闭。
- 合并开头连续指令；中途指令保留位置并转换为用户上下文，记录来源及语义变化。
- request.json 保留原始字节，forwarded_request.json 保存实际转发输入，record.json 保存转换审计。
- 自动 SFT 与离线导出使用实际转发输入，旧记录保持兼容；Responses 历史处理保持原样。
- 新增 Linux restart_proxy.sh，支持识别本项目旧代理、SIGTERM 停止和 nohup 后台重启。
- 新增 CHAT_COMPAT.md 更新方案；本次没有新增或运行测试，尚未在用户服务器验证。

# v2

- 新增原生 `/v1/responses` JSON/SSE 转发与事件重组，保留 Chat Completions。
- 支持普通 function tools、并行参数分片、function_call_output 的 call_id 关联。
- SQLite 持久化 response ID 索引，支持 previous_response_id 链与分支、重启恢复、原始记录重建。
- 终止事件发给调用方之前发布历史索引，处理立即发起下一轮的并发时序。
- 新增 GET/DELETE/cancel 生命周期透传及后台结果观察。
- 同时自动生成通用 SFT 和完整原生 Responses JSONL；导出支持原生格式、去重及后补祖先恢复。
- 顶层 instructions 采用每轮独立语义；对无法解析的服务器历史不猜测、不标为完整样本。
- 新增真实两轮工具调用检查脚本与 Linux Responses 部署指南。
- 部署目录统一为 `/mnt/data/zht/qwen-collector-proxy`，同步 Conda、数据目录和 systemd 用户服务。
- 测试从 19 项扩展到 38 项。
