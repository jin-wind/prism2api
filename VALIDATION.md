# 验证记录（2026-09-16）

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
