# 验证记录

## v0.3.3（2026-09-17）：上游改版导致的 HTTP 400，及**首次完整实网端到端通过**

当日上游改版：模型 `gpt-6-astra` → **`gpt-5.6-sol`**，项目 ID 也更换。
服务器上 9/16 抓的 template 因此失效，`model.start` 收到 HTTP 200 但内含应用错误。

定位过程（每一步都是实网实测，不是推断）：
- 诊断此前把所有未映射的失败都压成 `upstream_reason: unknown`，无法区分。
  改为**安全透传**：`reason`/`rootCause` 仅当形如「1–6 个不超过 16 字符的词」才回显
  （放行 `sandboxUnavailable`、`project_edit_access_required`，拦截 JWT、API key、UUID、
  用户 ID）；`httpStatus` 为有界整数直接回显；另附响应的**键名**（不含值）。
  自由文本 `message` 与 `codexRequestDebug` 仍然完全不回显。
- 由此拿到 `upstream_http_status: 400` —— 是请求被拒，不是之前记录的 500/502/503 不稳定。
- 新 HAR 对比确认模型名与项目 ID 变更。
- 新增 `export-template --har H --out F`：此前没有受支持的 template 刷新方式，
  只能手改 JSON，这正是本次故障恢复缓慢的原因。模型 id 改为从 template 派生
  （原先硬编码在 4 处）。

**实网端到端验证（部署在 144.79.170.102，真实 Prism + 真实凭据）**：

| 测试 | 结果 |
|---|---|
| 单轮文本 | HTTP 200，19.2s，输出 `bridge-e2e-ok`，`model.start/ok → model.poll/ok → turn/ok` |
| function 工具两轮闭环 | 轮 1 提议 `get_time({"tz":"UTC"})`，轮 2 回传结果后给出最终答案；各 11.4s |
| SSE 流式 | HTTP 200，事件序列 `response.created → in_progress → output_item.added → content_part.added → … → output_item.done → completed`，载荷 `stream-ok` |

这是本项目首次在**真实上游**下完成文本、工具闭环与流式三类调用的完整通过。
仍未验证：长时间运行稳定性、长上下文、多轮编码与文件写入。

验证：`python -m pytest -q` **112 passed**。

## v0.3.1（2026-09-17）：安全加固（采纳外部代码审查）

外部审查者基于 v0.2.2 提交了一份改动，其中的安全发现已采纳，两处"仅限 loopback"
的限制按本项目的公网部署方式改造后采纳。

已修复的真实问题：
- **`/oauth*` 全部端点此前无任何认证**，且已随 v0.3.0 暴露在公网。实测确认任何人都能
  `GET /oauth/login` 取得授权链接、`POST /oauth/submit` 把自己的 OpenAI 账号绑定到本桥接。
  现在除 `/oauth/callback` 外全部要求 bridge key；`/oauth/callback` 由 PKCE state 保护
  （state 只能由持密钥者通过 `/oauth/login` 生成）。
- `TrustedHostMiddleware` 从 `allowed_hosts=["*"]` 改为默认仅 loopback，公网主机须经
  `--public-host` / `PRISM_BRIDGE_PUBLIC_HOST` 显式声明。实测：未声明的 Host 返回 400，
  已声明的返回 200，声明一个不会重开通配。
- 旧 `/oauth` 页面 `return html` 返回裸字符串，被 FastAPI 序列化成 JSON，页面一直是坏的；
  该页每次 GET 还会新建一个 PKCE pending 会话（公网可达时为无界增长）。现改为 307 跳转
  到 `/ui#login`，该 tab 功能更全且会携带密钥。
- 凭据导入接口加入 64 KiB 上限、Content-Length 校验和严格字段白名单（多传 `path` 之类
  字段直接拒绝，而不是忽略）。
- 管理端点响应加 `X-Content-Type-Options`、`Referrer-Policy`、CSP（含 `frame-ancestors 'none'`）。

一并采纳的功能：`reasoning effort = "max"`，上游映射为 `output_config={"effort":"max"}`
并移除 `reasoning_effort`（两者同时出现会被上游拒绝）。该键名差异源自外部审查者的观察，
本轮未独立用真实 Prism 验证。

未采纳：外部版本的 `/admin` 面板（功能是本项目 `/ui` 控制台的子集）与其 `safe_status()`
（`admin.py` 已有等价的脱敏逻辑）。

验证：`python -m pytest -q` **110 passed**；UI JavaScript 通过 Node 语法检查；
本地实测（端口 8899）：`/oauth/status` 与 `/oauth/login` 无密钥 401、带密钥 200，
`/oauth` 307 跳转，`/ui` 200，声明的公网 Host 200，伪造 Host 400，`/v1/models` Bearer 200。

## v0.3.0（2026-09-17）：Web UI 控制台

- 新增 `/ui` 控制台（单文件、无构建步骤）：总览、三种登录方式、流量、诊断、Codex 接入。
- 新增 `obs.py`（有界流量记录，仅元数据）与 `admin.py`（`/ui/api`，要求 `X-Bridge-Key` 头）。
- `serve` 允许未认证启动，之后在 UI 登录；`/v1` 的 Bearer 认证与拒绝浏览器来源行为不变。
- 项目改为 git 管理；`deploy/` 一次性脚本、发布压缩包和含凭据文件移入 `archive/`（gitignored）。
- 验证：`python -m pytest -q` **102 passed**（原 93 项回归 + 9 项 admin/UI 测试）；
  内嵌 UI JavaScript 通过 Node 语法检查；真实启动冒烟（端口 8899）：
  `/ui` 200、无 key 401、`/ui/api/status` JSON 正确、`/v1/models` Bearer 200、未认证启动成功。
- 未做：真实 Prism 凭据下通过 UI 完成登录的实网验证；React/Vite 版本 UI（当前为无构建单文件）。

# 历史验证记录（2026-09-16）

## 已验证

- `python -m pytest -q`：**70 passed**（含 Cookie 轮换、session 刷新、私有工具续接及分阶段诊断）。
- `python -m compileall -q prism_bridge scripts`：通过。
- `prism_bridge.config.toml`：Python `tomllib` 解析通过。
- 两个 PowerShell 启动脚本：PowerShell AST 语法检查通过。
- 原 HAR 离线检查：490 条请求、7 次 Prism start；start 请求无 Cookie、无原生 `tools` 字段。
- 交付源文件检查：没有发现 HAR 中真实 sandbox token、projectId、userId 被复制到源码。
- WebCodex 参考仓库保持 clean，没有修改、提交、推送或部署。

## 真实 Codex CLI + 合成模型上游

可执行文件版本：`codex-cli 0.154.0`。

| 测试 | 请求轮数 | 结果 |
|---|---:|---|
| 文本回复 | 1 | 通过 |
| namespaced function：`clock.sleep` | 2 | 本地执行并回传结果，通过 |
| namespaced custom：`functions.exec → clock__curr_time` | 2 | 本地执行并回传结果，通过 |

使用临时 `CODEX_HOME`、临时工作目录和回环地址；未用真实 Prism 凭据。
合成后端只返回固定的测试文本或预先定义的只读工具请求，不调用真实模型。

较早的 `apply_patch` 集成尝试确实到达本地执行器，但被其 read-only sandbox 拒绝。
没有修改权限去强行通过；最终集成测试改为只读 custom tool。
单元测试覆盖独立 `apply_patch` custom tool 原始补丁输入的序列化和回传，不代表验证了文件写入权限。

环境提示：pytest 中出现一条 Starlette 关于 httpx 测试客户端的弃用提示；最后一组 CLI 测试关闭连接时，Windows asyncio 输出一次 `WinError 10054` 回调提示。三组 CLI 测试均退出码 0，工具结果验证均通过。未屏蔽这些提示。

## 真实 Prism 测试（与上面的合成上游测试分开）

| 测试 | 实际结果 |
|---|---|
| GET `/auth/session` / entitlements | 已登录、非匿名；成功 |
| POST `/auth/session` | 成功刷新；session 和 Cloudflare Cookie 发生轮换 |
| 单独用所提供的 Prism access token 导入 | 不带旧 Prism session 或其他原始 Cookie，也成功创建已登录会话 |
| 本地保存 CookieJar 后恢复 | 已实现、单元测试及 auth-status 路径验证 |
| 沙箱初始化、资源令牌及 Y 同步 | 有成功样本，后续也出现 5xx / 超时 |
| 原始 Prism 文本请求 | 返回 `prism-live-text-ok`，成功 |
| JSON 桥接文本请求 | 协议校验通过，约 40.5 秒 |
| 真实 Codex `clock.sleep` | 真实上游提议、本地执行、结果进入下一轮均已验证；第二轮上游 500 |
| 真实 Codex `functions.exec → clock__curr_time` | 真实上游提议、本地执行、结果进入下一轮均已验证；原生 snapshot 续接后的第二轮上游 500 |
| CookieJar 修正后的自定义工具重测 | 上游 start 约 220 秒后返回应用错误，内部为 502；未宣称成功 |
| access-only 认证状态、重新同步资源后重测 | 资源 token 注册出现 HTTP 错误和 ReadTimeout；未进入成功模型轮次 |
| 新的交互式 Codex OAuth 登录 | 脚本已提供，未代替用户进行登录，未验证该客户端 token 对 Prism 的兼容性 |

最后一组 HTTP/2 测试的 custom 阶段，在收到新 HAR 后停止了**隔离测试目录的 Codex 子进程**；该阶段是中断，不是成功。

完整新 HAR：`fuill.prism.openai.com.har`，408 条记录，香港时间 21:24:11–21:26:44。
它没有任何 LLM start 请求；两次 `/api/backend/1/new` 都是 `net::ERR_ABORTED`，其中一次约 60 秒，与前端沙箱获取超时阈值吻合。
另有两个 HTTP 503，正文为 `upstream connect error or disconnect/reset before headers. reset reason: connection termination`。
这说明浏览器自身也经历了初始化/上游连接问题，不能仅归因于桥接器，也不能据此确定具体服务端根因。

## 本轮修正

- `cdx1_` 会话前缀、Server Action 注册、workspace UUID 派生。
- 初始 system/editor-context 请求结构。
- `previousResponseId`、原样 `codexListenSnapshot` 及一次性本地 tool-call 关联。
- CookieJar 替代固定 Cookie 头，GET/POST session 刷新、并发刷新合并、原子持久化、账号匹配。
- 支持 session/CLIProxyAPI/Codex access-token 文件的显式导入，并强制 Prism 探测后才保存。
- 提供隔离的官方 Codex OAuth 登录脚本，不误用 Codex refresh token 为 Prism refresh cookie。
- 去掉 CLI 0.154.0 严格校验不接受的 `model_supports_reasoning_summaries` 字段。
- 补齐资源/Y 同步、心跳和状态观察的有限重试。没有自动重放传输失败的 start。

## 尚未完成

- 长时间运行、长空闲后的多轮稳定性（真实 function/custom 两轮成功样本见下面 v0.2.2 记录）。
- 新的 Codex OAuth 凭据能否被 Prism 接受，以及其跨客户端自动续期。
- 长上下文、多轮编码、文件写入与桌面应用接入的真实验证。

交付状态：**认证和本地协议已验证，真实工具执行有成功证据，完整多轮服务仍受上游错误影响**。不是已经完成的生产级服务。

## v0.2.1：针对用户截图的 ReadTimeout 诊断

- 保留用户正在运行的服务与 Codex 窗口，未杀进程或重启。
- 首次独立诊断：auth 正常；沙箱获取成功耗时 62.8 秒；Y 凭据和项目资源 token 获取成功；在沙箱资源 token 注册阶段等到总计 115 秒的诊断时限。未提交模型请求。
- 新增 `doctor`（仅认证/项目检查）与 `doctor --provision`（可选沙箱初始化）；输出阶段、耗时、HTTP 状态、是否发送模型请求。
- 新增认证保护的 `/diagnostics` 和有界 40 条内存事件记录；不记录凭据、正文或真实项目/用户标识。
- 修复原先所有传输错误都声称“start 未重试”的误导文本，区分尚未发模型请求、已提交但结果未知、已接受任务。
- 根据前端代码对 `resourceBaseUrl` 保留末尾斜杠；这项修正不能被当作已解决上游稳定性的证明。
- 诊断中发现本机 HTTP/TLS 栈可能直接抛出 `anyio.EndOfStream`；现归类为不泄漏敏感信息的传输错误而不是 Python traceback。
- 最终 `doctor` 的认证和项目访问检查通过，均约 1 秒；单元测试 70 项通过。
- 随后的 `doctor --provision`：认证和项目访问再次成功，`sandbox.acquire` 在 90.015 秒 ReadTimeout；报告明确 `model_request_sent=false`。本轮没有宣称修复了远端沙箱可用性。

## v0.2.2：长空闲生命周期检查和业务错误日志

用户日志中，一轮 14:51:41 UTC 成功；约 32 分钟后，15:23:53 与 15:24:14 两次 `model.start` HTTP 200 后出现 `prism_task_error`。
日志没有保存失败正文，因此不能断言这两次一定由沙箱过期引起。
代码审查确认：旧版只在任务期间跑心跳，并忽略心跳失败状态；下一轮主要依赖资源 JWT 到期时间，存在长空闲后未检查沙箱生命周期的缺口。

本轮变更：
- 每次模型提交前进行同步心跳检查，区分明确失效和暂时不可用。
- 只在模型提交前、收到明确失效证据后重建一次；注册资源/Y 凭据并同步。
- 记录不公开的 token 绑定和 sandbox session 代次，拒绝跨代次的工具 continuation。
- HTTP 200 应用失败不再被先记录为模型成功；白名单输出业务 reason/上游 500–504 分类。
- 保留之前的诊断、部署 fixture、认证和模板功能，没有改动部署脚本、凭据、主 Codex 配置或现有服务进程。

验证：
- 完整单元测试 **93 passed**，其中新生命周期/错误回归 23 项。
- 回归包含：正常一轮后模拟远端失效但资源 JWT 仍有效、单次重建失败、普通 5xx/超时不冒充失效、会话代次变化、旧工具续接拒绝、模型不重复提交、检查超时和错误脱敏。
- **真实 Prism + 真实 Codex CLI 的 function 测试完整通过**：`clock.sleep`，2 轮，结果回传与最终确认均成功；脚本总计 100.94 秒（含 CLI 启停等开销）。
- 真实网络测试使用单独复制的认证状态和临时工作目录，没有覆盖用户正在使用的状态文件。

这次没有等待真实的 32 分钟来宣称复现了原始故障；空闲失效分支由确定性 MockTransport 回归验证。上游未来仍可能返回业务或服务错误，新日志会保留可用分类以便进一步定位。
