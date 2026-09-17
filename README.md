# Prism → 本地 Codex Responses 适配器（实验版）

**已实网验证登录/刷新、access-token 导入，以及真实 Codex function/custom 工具的完整两轮闭环。仍是实验版，不保证上游持续可用。**
Prism 上游曾返回 500/502、传输中断；浏览器完整 HAR 也记录了 503 和沙箱初始化超时。不能把这些测试称为完整端到端成功。
登录方式、OAuth 与 session 的区别见 [AUTH.md](AUTH.md)。

## 接的到底是哪层 API？

```text
本地 Codex
  POST http://127.0.0.1:8765/v1/responses
       ↓ 适配器：序列化 instructions / tools / history
Prism 网页内部接口
  POST https://prism.openai.com/api/llm/response_with_tools_start
  POST https://prism.openai.com/api/llm/response_with_tools_status
       ↓ 远端 agent 返回约定的 JSON 文本（已有实网成功样本，稳定性仍需验证）
适配器：校验工具名、参数、nonce，转换 Responses SSE
       ↓ function_call / custom_tool_call
本地 Codex 执行工具（原有权限与沙箱继续生效）
       ↓ function_call_output / custom_tool_call_output
下一轮 /v1/responses
```

这里反向适配的是 **Prism 浏览器可见的 agent API**，不是获取其服务端到模型的内部地址或 API key。
HAR 中 7 次启动请求没有 `tools` / `tool_choice`，工具进度则由远端沙箱产生。
因此工具部分采用**结构化文本协议模拟**，并非已发现 Prism 支持原生本地 function calling。

## 已支持

- `POST /v1/responses`：普通 JSON 响应、SSE 响应。
- `GET /v1/models`；有限期内存存储的 `GET /v1/responses/{id}`。
- 普通 `function` 工具与 free-form `custom` 工具，例如 `apply_patch`、`functions.exec`。
- 新版 Codex 的 `input[].type = additional_tools`、`namespace` 工具声明及 namespaced 输出。
- `function_call_output` / `custom_tool_call_output` 与 `call_id` 匹配。
- 完整历史、`previous_response_id`；`store=false` 不保存响应，调用方须继续发送完整历史。
- 工具参数 JSON Schema 校验、工具白名单、`tool_choice`、并行调用数量约束。
- 轮询时更新 `turn_state`；SSE 客户端取消/绝对超时后尽力调用 stop。
- 本地工具结果回传时保留私有 `previousResponseId` / `codexListenSnapshot`，不把它们泄漏给 Codex 客户端。
- CookieJar、`GET/POST /auth/session`、原子持久化、账号匹配验证；读取显式指定的 session/Codex/CLIProxyAPI access-token 文件。
- 沙箱初始化、项目资源令牌与 Y 同步、心跳；只对状态观察做有限传输重试，不自动重放超时的 start。
- 默认只监听回环地址、独立本地 Bearer key；不输出 HAR 令牌、Cookie、上游错误正文。

**桥接程序本身不执行本地工具，也不解析/执行工具返回文本中的命令。**
工具调用是标准 Responses item，由 Codex 客户端决定是否允许以及在哪里执行。

## 当前边界

1. **完整工具闭环已有成功样本，但不保证稳定性**：真实 function/custom 工具、结果回传和最终回答都已通过；历史测试仍出现过 Prism 500/502/超时。
2. Prism 的原生工具无法通过已观察到的 API 硬关闭。提示词要求不执行远端工具；检测到原生工具事件则报错并尽力停止。这种检测不保证能阻止已发生的远端执行。
3. SSE 是收到完整上游答案后的事件转换；等待期间发送心跳，**不是原生 token 流**。
4. 新任务通过抓包中的 `createProjectConversation` Server Action 注册 `cdx1_…` 会话；`workspace_session_id` 是后面的 UUID。工具循环复用返回的快照和上游 response ID，单沙箱串行运行。Server Action 随部署变化时需要更新 HAR。
5. 不支持图片、文件输入、hosted web search、远端 MCP 声明、加密 reasoning 恢复、`/responses/compact`、background mode 或 JSON-schema 最终答案约束。明确报错，不伪装支持。通过普通 function/custom 工具暴露的本地 MCP 调用可以按其声明处理。
6. 自定义工具的 Lark/regex grammar 会转发给模型，但桥接端不执行该 grammar；真正工具执行仍由 Codex 校验。函数参数的 JSON Schema 在桥接端验证。
7. 不报告虚构的 token 用量：`usage=null`。Codex UI 可能把未知用量显示为 0。
8. 不重新创建 Prism 项目、不重放传输失败的 start，也不保证完整 OpenAI API 兼容。Prism session 刷新不等于能够无限刷新任意来源的 OAuth access token。
9. 内存响应记录最多 100 个 / 32 MiB / 1 小时；重启后消失。请求和历史各限 4 MiB。
10. 保留浏览器初始 system/editor-context 的请求形状；桥接协议同时放入 user 文本载荷。已有真实 JSON 工具提议样本，不能据此保证所有任务均遵循。

## Windows 启动

需要 Python 3.11+ 和支持 Responses 自定义 provider 的 Codex。

```powershell
cd D:\Code\prism-codex-bridge
python -m pip install -e '.[test]'
python -m prism_bridge inspect --har 'D:\下載\prism.openai.com.har'
```

在本地创建 `.local\prism-cookie.txt`，内容是当前 Prism 请求的完整 **Cookie 头值**，一行即可；也可带 `Cookie:` 前缀。
取值位置：浏览器开发者工具 → Network → 已登录的 Prism 请求 → Request Headers → Cookie。
此文件仅用于本机读取。也可以按 AUTH.md 导入 access token，随后仅使用本地认证状态文件，不需要原始 Cookie 文件。默认启动会重新初始化沙箱和资源凭据。

终端 1：

```powershell
.\Start-Bridge.ps1 -HarPath 'D:\下載\prism.openai.com.har'
```

终端 2，从准备让 Codex 操作的项目目录启动：

```powershell
& 'D:\Code\prism-codex-bridge\Use-Codex.ps1'
```

脚本只在用户 `CODEX_HOME` 下添加独立 `prism_bridge.config.toml`；不改主 `config.toml`、`auth.json` 或已有不同内容的同名 profile。
桥接 key 保存在 `.local\bridge-key.txt`，启动 Codex 时注入 `PRISM_BRIDGE_API_KEY`。
默认端口为 8765；如更改服务端端口，需同步修改 profile 的 `base_url`。

如果 Codex 不在 PATH：

```powershell
& 'D:\Code\prism-codex-bridge\Use-Codex.ps1' `
  -Codex 'C:\Users\micha\AppData\Local\Programs\OpenAI\Codex\bin\codex.exe'
```

这套启动脚本已按 **CLI** 设计。桌面版是否使用同一 profile、如何继承环境变量需单独核对，没有自动改桌面应用配置。

## 配置摘要

见 `prism_bridge.config.toml`：`wire_api="responses"`、`supports_websockets=false`、`web_search="disabled"`。
`gpt-6-astra` 来自 HAR 中请求 metadata；这不是后端实际模型身份的独立验证。

直接运行服务时的环境变量：

| 变量 | 用途 |
|---|---|
| `PRISM_HAR_PATH` | 提供 project/user/sandbox metadata 的本地 HAR |
| `PRISM_COOKIE_FILE` | 初始浏览器 Cookie 文件；已保存认证状态时可省略 |
| `PRISM_AUTH_STATE` | 私有 CookieJar 状态路径，默认 `.local/prism-auth.json` |
| `PRISM_BRIDGE_API_KEY` | 本地客户端认证 key，至少 16 字符；不是 OpenAI API key |
| `PRISM_TURN_TIMEOUT` | 包括排队在内的单轮总超时秒数，默认 300 |
| `PRISM_HTTP_READ_TIMEOUT` | 上游 HTTP 单次读取等待秒数，默认 90；不改变总时限 |

```powershell
python -m prism_bridge serve
```

## 遇到 `ReadTimeout` / `stream disconnected before completion`

这条 Codex 提示也可能是桥接器发送 `response.failed` 后被显示出来，不一定代表 Codex 到本机端口断线。
v0.2.1 起，上游错误包含确切阶段和耗时，例如：

```text
Prism sandbox.resources.register failed (ReadTimeout) after 90.0s.
No model request was sent.
```

服务终端会打印不含 Cookie、token、请求正文、项目 ID 的阶段日志；诊断接口也需要本地 API key。

```powershell
cd D:\Code\prism-codex-bridge

# 只检查认证和项目访问，不发模型请求、不创建沙箱。
python -m prism_bridge doctor --har 'D:\下載\prism.openai.com.har'

# 显式检查沙箱初始化/同步；最多 120 秒，仍然不发模型请求。
python -m prism_bridge doctor --har 'D:\下載\prism.openai.com.har' --provision --deadline 120
```

`doctor` 失败时返回非零退出码及结构化结果。若卡在 `sandbox.acquire` 或 `sandbox.resources.register`，增加 Codex 的 `stream_idle_timeout_ms` 不能修复那个上游请求。
诊断成功只能证明诊断阶段成功，不能保证后续模型或工具轮次一定成功。

修改源码后，正在运行的服务不会自动重载：只在 **Start-Bridge 所在终端**按 Ctrl+C，再执行原启动命令即可；无需关闭 Codex 窗口，也不要反复重发可能已被上游接受的模型任务。

## v0.2.2：空闲后失败与 HTTP 200 业务错误

- 每轮提交模型前，先同步检查沙箱心跳。后台心跳不再作为唯一的存活依据。
- 明确的 404/410，或带 `x-crixet-sandbox-expired: true` 的 502，才触发一次沙箱重建，并重新注册资源/Y 凭据、等待同步。
- 普通 5xx、401、连接超时不被猜测为“已过期”：模型不会提交，日志明确停在 `sandbox.check`。
- `x-session-id` 发生变化时先重同步。旧工具 continuation 不会被接到新沙箱；返回 `sandbox_continuation_expired`，且不提交模型。
- 心跳不会在任务进行中更换沙箱。资源检查、重建及同步共用整轮绝对时限；不会无限重建。
- HTTP 200 包裹的应用错误记录为 `phase: response`，随后记录 `failed`，不再先写成成功。
- 应用错误仅输出白名单分类，例如 `upstream_reason: unknown`、`upstream_category: upstream_service_error`、`upstream_status: 500`。不输出原始错误正文或凭据。
- 传输失败、已接受或结果不明的 start 不会自动重新提交。

例如以下日志代表传输成功，但上游应用失败：

```json
{"stage":"model.start","phase":"response","http_status":200,"upstream_state":"completed","upstream_reason":"unknown","upstream_category":"upstream_service_error","upstream_status":500}
```

如果日志来自 Docker/VPS，更新本地文件不会改变那个已运行实例；需把新版源码更新到对应环境并重启桥接服务。此次修复没有重启或部署用户的现有服务，也没有更改认证文件或主 Codex 配置。

## 验证

```powershell
python -m pytest -q
python scripts/codex_smoke.py --codex 'C:\path\to\codex.exe' --mode all
```

第二个测试调用真实 Codex CLI，但**模型上游是进程内的合成后端**，不会访问 Prism。
完整验证结果及未验证项见 `VALIDATION.md`；本次单元测试为 **93 passed**。
测试使用临时 `CODEX_HOME` 和工作目录；覆盖文本、function call、本地 custom tool 与结果回传。
本机 CLI 0.154.0 通过 `clock.sleep` 和 `functions.exec → clock__curr_time` 验证。
最初写入测试被本地 read-only sandbox 拒绝，未放宽该权限；改用只读 custom tool 验证协议。

## 参考 WebCodex 的部分

参考仓库：<https://github.com/yyjeqhc/webcodex>
查阅提交：`dd93401eb6135b8e299963b27b5fac670f0d0b5d`。

其架构是 **MCP/HTTPS Server + 本地 Runner**，不是网页模型转 OpenAI API 代理。
本项目参考了工具身份精确匹配、参数校验、结果与调用关联、权限留在执行端等原则，未复制其 Rust 运行时或把它当成 Prism 模型客户端。

查阅文件：
- `src/mcp/tools.rs`
- `crates/webcodex-tool-runtime-contracts/src/tool_call.rs`
- `crates/webcodex-tool-runtime-contracts/src/tool_result.rs`
- `docs/agent/tool-contract-guidelines.md`

官方文档：
- <https://developers.openai.com/codex/config-reference>
- <https://developers.openai.com/codex/config-advanced>
- <https://developers.openai.com/api/docs/guides/function-calling>

源包不包含参考仓库、原始 HAR、Cookie 或其他真实凭据。
