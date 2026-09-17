# 登录与会话管理

## 已验证的 Prism 会话路径

本次实网确认：

1. `GET /auth/session` 解析会话，可能同时更新 Cookie。
2. `POST /auth/session` 是前端 `refreshPrismAuthState()` 的实际刷新接口。
3. 服务器会轮换 `prism_session_token`、`__cflb`、`__cf_bm`。固定发送旧 Cookie 头会忽略这些轮换，因此桥接器现使用真正的 CookieJar。
4. 从用户提供的 Cookie 中单独提取其 `prism_oai_access_token`，不带旧 `prism_session_token` 或其他原始 Cookie，调用 `/auth/session` 也成功建立了已登录 Prism 会话。

第 4 项验证的是**该用户提供的、原本用于 Prism 的 access token**；不能据此宣称任意 ChatGPT/Codex token 都可互换。

## Cookie 初始化 / 刷新

```powershell
python -m prism_bridge auth-refresh `
  --har 'D:\下載\prism.openai.com.har' `
  --cookie-file '.local\prism-cookie.txt'
```

更新后的 Cookie（含 domain/path/expiry）原子写入 `.local/prism-auth.json`，不会打印凭据。
服务会使用已验证的 CookieJar，并按服务器提示的时间定期检查会话；遇到明确 HTTP 401 时，按前端行为刷新后重发一次。不会将传输超时当成 401 自动重发 start。

```powershell
python -m prism_bridge auth-status --har 'D:\下載\prism.openai.com.har'
```

这会优先使用本地已保存状态，不需要再传旧 Cookie 文件。

## 导入 OpenAI session / CLIProxyAPI / Codex 文件

支持读取下列三种显式指定的本地 JSON 格式：

```json
{"accessToken":"<session access token>"}
```
```json
{"access_token":"<CLIProxyAPI access token>"}
```
```json
{"tokens":{"access_token":"<Codex access token>"}}
```

```powershell
python -m prism_bridge auth-import `
  --har 'D:\下載\prism.openai.com.har' `
  --auth-file 'D:\path\to\session.json'
```

导入时会移除旧的 Prism 身份/恢复 Cookie，再用新 access token 向 Prism 验证，避免“实际靠旧账号登录成功”的假阳性。
只有 Prism 返回非匿名用户、且与 HAR 的账号身份匹配，才保存状态；不会自动扫描本机的其他凭据文件。

**格式可导入 ≠ 该 token 已被 Prism 接受。** Codex/ChatGPT token 的兼容性由上述实网探测决定。

## Codex OAuth 登录入口

参考 CLIProxyAPI 的 PKCE、device flow、刷新串行化和持久化设计；实际登录委托已安装的官方 Codex CLI，避免维护第二套浏览器回调/PKCE 实现。

```powershell
.\Login-CodexOAuth.ps1 `
  -HarPath 'D:\下載\prism.openai.com.har' `
  -Codex 'C:\Users\micha\AppData\Local\Programs\OpenAI\Codex\bin\codex.exe'
```

需要设备登录时加 `-DeviceAuth`。只想获取独立 Codex OAuth 凭据而不测试 Prism 时加 `-AcquireOnly`。

- 登录使用隔离的 `.local/codex-oauth`，不替换用户平时的 Codex 登录。
- 登录需用户在官方页面完成交互；本次没有代替用户执行新的交互式 OAuth 登录。
- 默认随后尝试导入新 access token，并要求 Prism 实际验证成功。
- 成功后的独立状态文件为 `.local/codex-import-prism-auth.json`，不会替换已有 Prism 状态。

```powershell
.\Start-Bridge.ps1 -HarPath 'D:\下載\prism.openai.com.har' `
  -AuthState '.local\codex-import-prism-auth.json'
```

## 不混用 refresh token

CLIProxyAPI 的 Codex 客户端是 `app_EMoamEEZ73f0CkXaXp7hrann`；提供的 Prism access token 中记录了不同的 client ID。
因此本桥接器**不会**把 Codex `refresh_token` 填成 `prism_oai_refresh_token`，也不会用错误的客户端去兑换它。

目前实现：Prism session 刷新、Cookie 轮换、access-token 导入及验证、官方 Codex OAuth 登录入口。
未实现/未验证：Codex refresh token 自动兑换后持续转接 Prism、任意 OpenAI session 与 Prism 的通用互换。

用户此次提供的 Cookie 中没有 `prism_oai_refresh_token`。仅有 access token 时，不能承诺在其最终过期后仍可无限续期；届时需由所属登录客户端刷新或重新登录再导入。

## 参考资料

- CLIProxyAPI：<https://github.com/router-for-me/CLIProxyAPI>，查阅提交 `8335eac731946bd4eff18f500653f93736df53d6`。
- `internal/auth/codex/openai_auth.go`：PKCE、token exchange、refresh singleflight。
- `sdk/auth/codex.go`、`sdk/auth/codex_device.go`：浏览器与设备登录。
- `internal/auth/codex/token.go`：凭据存储字段。
- OpenAI 官方文档：<https://developers.openai.com/codex/auth>。

参考仓库未修改；这里没有复制其 Go OAuth 实现。交付包不包含 `.local` 或真实凭据。
