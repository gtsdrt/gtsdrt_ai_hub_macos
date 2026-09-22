# 安全策略

## 支持的版本

| 版本 | 安全更新 |
|---|---|
| `0.2.x`（当前） | ✅ |
| `< 0.2` | ❌ 不再维护 |

## 上报漏洞

**请不要开公开 issue。** 请优先使用 GitHub 的私有漏洞报告（Private Vulnerability Reporting）：

👉 <https://github.com/gtsdrt/gtsdrt_ai_hub_macos/security/advisories/new>

（仓库页 → **Security** 标签 → **Report a vulnerability**）

如果无法使用该渠道，可以先开一个**不含任何技术细节**的 issue，只说明「有安全问题需要私下沟通」，我会再提供联系方式。

这是一个个人维护的项目，响应时间**尽力而为**，不承诺 SLA，也没有漏洞奖金：

- 72 小时内确认收到
- 7 天内给出初步判断与修复计划
- 修复后经你同意再公开披露

上报时请尽量包含：

1. 受影响的版本 / 提交哈希，以及运行方式（源码运行，还是 DMG 安装包）
2. 复现步骤或 PoC
3. 影响评估（能读到什么、能改到什么、需要什么前置条件）
4. 是否已经公开或告知过其他人

## 范围内

- 后端 `main.py` / `tools/`：鉴权绕过、越权读取他人会话、路径穿越、命令注入、SSRF 等
- **prompt injection 导致工具被越权调用**（例如用恶意文件内容诱导模型把云资产信息读出去）
- 客户端 `AIChatApp/`：Keychain 使用、`.env` 与本地文件权限、客户端与本地后端之间的信任边界
- 凭据泄漏：把密钥写进日志、崩溃报告、错误响应或提交进仓库
- 直接依赖中的已知漏洞（`requirements-fastapi.txt`）

## 不在范围内

- 把后端暴露到公网且未套 TLS / 反向代理 / 访问控制，或未修改默认口令
- Azure、Meraki、Google、GitHub、DeepSeek、Kimi 等第三方 API 自身的问题（请报给对应厂商）
- 需要「本机已经具备任意代码执行权限」才能利用的问题
- 社会工程、物理接触
- 模型输出内容本身（幻觉、错误的运维建议）；但**工具调用导致的越权或数据外泄属于范围内**

## 安全模型与设计取舍

为了让你判断上报的「预期行为」是否算漏洞，这里把设计意图写清楚：

1. **单机单用户工具**：后端默认服务本机客户端，没有多租户隔离、没有速率限制、没有审计日志留存。
2. **鉴权**：本地管理员口令 + 自签 JWT（HMAC-SHA256）。默认 `admin` / `password123` 与默认 `JWT_SECRET` 仅供本机开发，**部署前必须更换**。
3. **OAuth 白名单可以为空**：`GOOGLE_ALLOWED_EMAILS` / `GOOGLE_ALLOWED_DOMAINS` / `GITHUB_ALLOWED_LOGINS` / `GITHUB_ALLOWED_EMAILS` 全部留空时，**任何已验证的账号都能登录**。生产环境请至少配一个。
4. **工具是只读的**：`tools/` 中没有任何对 Azure / Meraki 的写操作（全部是 `list_*` / `get_*`），模型无法通过工具修改配置或删除资源。
5. **数据出站边界**：开启 `enable_tools` 时，工具返回的云资源信息会作为上下文发给 AI Provider。也就是说，**一次成功的 prompt injection 可以把你云资产的清单信息读给模型服务商看**。不接受这一点时，请不要开启工具，或改用本地模型。
6. **`webhook_url` 是调用方指定的任意 URL**（任务完成后 POST 回调），本质上是 SSRF 面；因为该接口要求有效 JWT，只有在多用户 / 公开暴露的场景下才具备实际风险。
7. **密钥存储**：本地 `.env`（已被 `.gitignore` 排除）与 macOS Keychain。历史文件 `function_app.py` 在开源前已脱敏：租户/订阅 GUID、Key Vault 与 Azure OpenAI 端点、内部网络命名均已替换为环境变量或占位符（`TENANT_SECONDARY_ID`、`SUBSCRIPTION_SECONDARY_ID`、`KEY_VAULT_URL`）。
8. **发布包是 ad-hoc 签名且未公证**：Gatekeeper 会拦截，需要用户手动放行（`xattr -dr com.apple.quarantine`）。这也意味着**发布包没有可验证的代码签名身份**，请用 Release 页面给出的 SHA-256 自行校验完整性。

## 部署加固清单

- [ ] `JWT_SECRET` 换成 32 字节以上的随机串，`ADMIN_PASSWORD` 改掉默认值
- [ ] `chmod 600 .env`（默认可能是 `0644`，同机其他用户可读）
- [ ] 只想本机用时设 `HOST=127.0.0.1`；需要远程访问请走 SSH 隧道或 VPN，不要把 8000 端口直接暴露
- [ ] 配好 Google / GitHub 登录白名单（邮箱或域名）
- [ ] Azure 服务主体只授予 **Reader**，Meraki 使用**只读** Dashboard API Key
- [ ] 定期轮换 AI Provider / Meraki / Azure 凭据
- [ ] 从 Release 下载安装包后核对 SHA-256

## 致谢

有效的漏洞报告在修复后会在 Release Notes 里致谢（可以选择匿名，或指定要附上的链接）。
