# 安全策略

## 支持的版本

| 版本 | 安全更新 |
|---|---|
| `>= 0.3.10`（当前） | ✅ 已修复下列鉴权问题 |
| `0.2.x` – `0.3.8` | ⚠️ **受影响**，存在无凭据鉴权绕过（见下），请尽快升级到 `0.3.10` 或更高 |
| `< 0.2` | ❌ 不再维护；同样受影响 |

> **升级到 `0.3.10` 之前的所有版本都请尽快升级。** 这些版本存在三个相互独立的鉴权问题：
> `X-MS-CLIENT-PRINCIPAL` 请求头可无条件通过鉴权、`JWT_SECRET` 有公开默认值、
> `ADMIN_PASSWORD` 默认为 `password123`。三者中任意一个都足以让能访问后端端口的人
> 在**没有任何凭据**的情况下取得完整访问权限。修复随 `0.3.10` 发布。
>
> 曾把后端绑到 `0.0.0.0` 或暴露到局域网的用户，应视为凭据可能已泄露，
> 并轮换 Azure / Meraki / Nexus Dashboard / AI Provider 凭据。
>
> 注意：`0.3.10` 起后端对「未设置管理员口令」拒绝启动，升级时请先在
> App 的「设置 → 本机管理员」里设置一个 ≥ 8 位的口令（详见该版本 Release Notes）。

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

- 把后端暴露到公网且未套 TLS / 反向代理 / 访问控制（后端默认只监听 `127.0.0.1`，且未设置管理员口令时会拒绝启动；主动对外暴露仍不在范围内）
- Azure、Meraki、Google、GitHub、DeepSeek、Kimi 等第三方 API 自身的问题（请报给对应厂商）
- 需要「本机已经具备任意代码执行权限」才能利用的问题
- 社会工程、物理接触
- 模型输出内容本身（幻觉、错误的运维建议）；但**工具调用导致的越权或数据外泄属于范围内**

## 安全模型与设计取舍

为了让你判断上报的「预期行为」是否算漏洞，这里把设计意图写清楚：

1. **单机单用户工具**：后端默认服务本机客户端，没有多租户隔离、没有速率限制、没有审计日志留存。
2. **鉴权（无默认口令）**：本地管理员口令 + 自签 JWT（HMAC-SHA256）。
   - `ADMIN_PASSWORD` **必须显式设置**；未设置 / 为空 / 仍是历史默认值 / 短于 8 位时后端**拒绝启动**（fail closed）。
   - `JWT_SECRET` 未设置时自动生成随机密钥并持久化到 `~/Library/Application Support/AIChatApp/jwt_secret`（0600），不再使用公开默认值。
   - `X-MS-CLIENT-PRINCIPAL`（Easy Auth）**默认不信任**；只有确实部署在可信反向代理后面才可开启 `AICHAT_TRUST_EASY_AUTH=1`，且必须同时设置 `AICHAT_EASY_AUTH_SECRET`，否则后端拒绝启动。
   - `/docs`、`/redoc`、`/openapi.json` 默认关闭（`AICHAT_ENABLE_DOCS=1` 打开）。
   - 登录接口**没有速率限制**，这是单机工具的已知取舍；不要把它暴露给不可信网络。
3. **OAuth 白名单可以为空**：`GOOGLE_ALLOWED_EMAILS` / `GOOGLE_ALLOWED_DOMAINS` / `GITHUB_ALLOWED_LOGINS` / `GITHUB_ALLOWED_EMAILS` 全部留空时，**任何已验证的账号都能登录**。生产环境请至少配一个。
4. **工具基本只读，但有一个执行入口**：Azure（21 个）与 Meraki（45 个）全部是 `list_*` / `get_*`，Nexus Dashboard 只读（仅登录用 POST）。**例外是容器执行器 `container_run_task`**，它会 `POST {url}/run-task`，是唯一的写 / 执行路径：受注册表 `allowed_tasks` / `mode` 约束，默认 `auto` 只放行容器自报 `read_only: true` 或内置只读名单中的任务，**容器明确标注 `read_only: false` 的任务一律拦截**。把 `mode` 设为 `all` 或让 `allowed_tasks` 含 `"*"` 等于把执行器完全交给模型（危险）。
5. **数据出站边界**：开启 `enable_tools` 时，工具返回的云资源信息会作为上下文发给 AI Provider。也就是说，**一次成功的 prompt injection 可以把你云资产的清单信息读给模型服务商看**。不接受这一点时，请不要开启工具，或改用本地模型。
6. **`webhook_url` 默认禁止访问内网**：任务完成后 POST 回调，只允许 http/https，且解析出的地址不能是回环 / 私有 / 链路本地 / 保留 / 组播地址；重定向逐跳校验，且不继承环境代理。确有需要时用 `AICHAT_ALLOW_PRIVATE_WEBHOOKS=1` 显式放开（放开即接受 SSRF 风险）。
7. **密钥存储**：本地 `.env`（已被 `.gitignore` 排除，App 写入时设为 0600）与 macOS Keychain。
   - 客户端把各 Provider / 云平台凭据作为**子进程环境变量**传给内嵌后端（不走命令行参数）。同机同用户的进程理论上可以读到这些环境变量，这是本机单用户模型下的已知取舍。
   - 历史文件 `function_app.py` 已脱敏为占位符。**注意**：早期版本曾在其中硬编码一个真实的 Ansible 执行器域名，该值仍保留在 git 历史里，因此该端点地址应视为已公开；建议轮换对应密钥并限制其入口。
8. **发布包是 ad-hoc 签名且未公证**：Gatekeeper 会拦截，需要用户手动放行（`xattr -dr com.apple.quarantine`）。这也意味着**发布包没有可验证的代码签名身份**，请用 Release 页面给出的 SHA-256 自行校验完整性。
9. **备份文件属于不可信输入**：恢复前会校验端点（本机后端必须回环；`openaiBaseURL` / `ndBaseURL` 非回环时必须是 HTTPS）与环境变量名白名单（防止注入 `DYLD_INSERT_LIBRARIES` 之类）。但这类校验无法阻止指向任意 `https://` 主机，**请只恢复你自己信任的备份**。

## 部署加固清单

- [ ] 显式设置 `ADMIN_PASSWORD`（≥ 8 位，非历史默认值）——不设置后端会拒绝启动
- [ ] `chmod 600 .env`（App 写入时会设成 0600，手工创建的文件请自己确认）
- [ ] 保持 `HOST=127.0.0.1`；需要远程访问请走 SSH 隧道或 VPN，不要把 8000 端口直接暴露
- [ ] 不要开启 `AICHAT_TRUST_EASY_AUTH`，除非前面确实有可信反向代理，并已配好 `AICHAT_EASY_AUTH_SECRET`
- [ ] 除非本机调试，保持 `AICHAT_ENABLE_DOCS` 关闭
- [ ] 配好 Google / GitHub 登录白名单（邮箱或域名）
- [ ] Azure 服务主体只授予 **Reader**，Meraki 使用**只读** Dashboard API Key
- [ ] 用 `AZURE_ALLOWED_SUBSCRIPTIONS` 限定可查询的订阅范围
- [ ] 容器执行器保持默认 `mode=auto`，只把确需的任务写进 `allowed_tasks`；**不要用 `mode=all` / `"*"`**
- [ ] Nexus Dashboard 尽量保持 `ND_VERIFY_TLS=true`；自签证书环境必须关闭时，确认网络路径可信
- [ ] 定期轮换 AI Provider / Meraki / Azure / Nexus Dashboard 凭据
- [ ] 从 Release 下载安装包后核对 SHA-256
- [ ] 只恢复你自己信任的备份文件

## 致谢

有效的漏洞报告在修复后会在 Release Notes 里致谢（可以选择匿名，或指定要附上的链接）。
