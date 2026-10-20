# v2

- 新增原生 `/v1/responses` JSON/SSE 转发与事件重组，保留 Chat Completions。
- 支持普通 function tools、并行参数分片、function_call_output 的 call_id 关联。
- SQLite 持久化 response ID 索引，支持 previous_response_id 链与分支、重启恢复、原始记录重建。
- 终止事件发给调用方之前发布历史索引，处理立即发起下一轮的并发时序。
- 新增 GET/DELETE/cancel 生命周期透传及后台结果观察。
- 同时自动生成通用 SFT 和完整原生 Responses JSONL；导出支持原生格式、去重及后补祖先恢复。
- 顶层 instructions 采用每轮独立语义；对无法解析的服务器历史不猜测、不标为完整样本。
- 新增真实两轮工具调用检查脚本与 Linux Responses 部署指南。
- 部署目录统一为 `/mnt/data/qwen-collector-proxy`，同步 Conda、数据目录和 systemd 用户服务。
- 测试从 19 项扩展到 38 项。
