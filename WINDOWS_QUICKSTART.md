# Windows 网页自动提问版：1000组题目

本版在可见浏览器中操作：打开数字员工运行页 → 逐字输入 → 点击页面发送按钮 → 等待页面回复 → 再问下一题。
默认入口 `start_windows.cmd` 启动 `batch_browser_questions.py`，不直接调用提问接口或向模型发送请求。
页面自己执行原有请求、工具调用和流式显示；服务器采集代理继续记录模型链路。

## 1. 使用步骤

1. 解压到固定文件夹，例如 `D:\qwen-collector-windows`。
2. 首次使用或从旧包升级后，双击 `setup_windows.cmd` 安装依赖。
3. 双击 `start_windows.cmd`，选择运行页地址、题库、执行数量、输出目录和浏览器。
4. 默认打开电脑上的 Microsoft Edge，也可选择 Google Chrome。
5. 在打开的浏览器中自己登录平台，进入数字员工运行页。
6. 回到控制台按回车，脚本才开始输入和发送。

脚本窗口和浏览器窗口都要保持打开；Ctrl+C中止。自动提问期间不要手动输入或点击该运行页。
可切换到其他应用，但电脑不要睡眠。需要 Python 3.10 或更高版本，不需要 CUDA、PyTorch 或 vLLM。

使用 [Playwright 支持的本机 Edge/Chrome](https://playwright.dev/python/docs/browsers#google-chrome--microsoft-edge)。
默认不下载浏览器，不接管你平时的浏览器配置。没有Edge时可选择已安装的Chrome。
如果选择 Playwright Chromium，先手动安装：

```powershell
.\.batch-env\Scripts\python.exe -m playwright install chromium
```

## 2. 会话内记忆与隔离

当前运行页源码没有独立“新会话”按钮，页面初始化时会自行生成新的UUID会话ID。

- 每组开始时完整重新打开运行页，由页面自己生成新的 session_id/threadId。
- 同组多轮留在当前页面继续输入，保留会话内短期记忆，不主动带入上一组历史。
- 被动观察页面已发出的提问请求，记录会话ID并检查跨组不复用、组内不变化。
- 不修改Vue内部状态、userKey或认证身份，不保存请求头和原始请求体。
- 平台后端也必须按session/thread保存记忆；脚本无法隔离用户级或全局共享记忆。

浏览器登录状态保存在独立的 `browser_profiles/wing/`，下次运行可保持登录。
该目录含浏览器登录状态，被Git忽略，不要上传或分享；登录状态不等于聊天历史。
浏览器配置目录有互斥锁，避免两份脚本同时使用。独立聊天会话也不等于独立MCP浏览器或业务数据库，
工具状态和业务数据隔离需要工具服务提供支持。

重新打开页面会清除临时填写的“前端调试工具”配置。本版不重建这些临时调试项；
框架服务端配置的工具按原方式加载。出现“前端工具中断”时停止整批，不填写模拟工具结果。

## 3. 控制请求负载

- 单窗口单并发；等待输入框恢复、发送按钮结束加载、助手消息出现并稳定后才继续。
- 每组间隔默认3秒；每组最多3轮，单轮问题最多4000字符。
- 页面错误提示、提问请求HTTP错误、超时、会话ID异常或前端工具中断时停止整批，不自动重发。
- 页面出现显存不足相关文字时停止整批；工具错误卡片出现后不继续本组后续追问。

网页模式不通过管理接口检查或修改配置，请在平台保持生成令牌数量不超过4096。
模型服务继续使用 `--max-num-seqs 1`，避免同时运行其他批量请求。
这些措施降低提交压力，不能保证外部GPU进程、超长工具结果或模型服务配置不会造成OOM。

## 4. 已附题库

| 题库 | 组数 |
|---|---:|
| `readonly.jsonl` 只读查询 | 300 |
| `form_draft.jsonl` 填写暂不提交 | 200 |
| `form_submit.jsonl` 保存/提交 | 200 |
| `dates.jsonl` 日期格式与范围 | 150 |
| `dialogue.jsonl` 缺参、重复与异常边界 | 100 |
| `web_search.jsonl` 联网搜索 | 50 |
| `all_1000.jsonl` 全部题目 | 1000 |

共1000组、1405次用户发言，基于200组任务样例扩展数据条件和自然语言表达。
默认只读题库；全部1000组包含200组真实保存/提交指令，应使用业务测试环境。
题目只包含用户自然语言，工具定义由框架提供，没有预置模型答案或虚构工具结果。
实际页面、字段和权限以可用工具为准；正确结果可能是缺参追问、空结果或错误说明。
同源变体用 source_case_id 标记，人工划分评测集时应放在同一分组。

## 5. 保存内容与断点

```text
data/browser/windows-<题库>-<日期>/
  qa.jsonl                     # 完成题组的页面问答
  cases/<题目指纹>/case.json   # 多轮状态、实际会话ID、题目分类
  cases/.../attempts/.../
    question.json              # 本轮输入内容
    request_observation.json   # 被动观察的会话ID和请求次数，无认证信息
    result.json                # 页面文本、返回的reasoning、工具卡片
    page.png                   # 结束或异常时截图，截图失败时可能缺少
```

同目录再次运行跳过已有记录，包括未完成题目，避免重复业务操作。
要重做旧题，人工确认后使用新的输出目录。页面结构变化导致定位失败时停止，不猜测点击其他页面。

Windows保存页面显示内容和截图；完整模型原始请求、流式响应及工具上下文仍由服务器采集器保存：

```text
/mnt/data/zht/qwen-collector-proxy/data/datasets/sft-YYYY-MM-DD.jsonl
```

确保数字员工模型调用经过采集代理。技术上完成不代表业务正确，训练前需要审阅。

## 6. PowerShell启动

```powershell
.\.batch-env\Scripts\python.exe -u batch_browser_questions.py --page-url 'http://平台IP:5177/agent-runtime/智能体配置ID' --input question_banks/all_1000.jsonl --output data/browser/run-001 --browser msedge
```

旧的 `batch_agent_questions.py` 接口模式仍保留，但不是Windows默认入口。
本次根据页面源码实现，未登录平台、未发送题目，也没有新增或运行测试。
