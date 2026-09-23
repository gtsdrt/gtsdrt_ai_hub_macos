# AIChatApp · macOS AI 运维工作台

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

一个**完全跑在你自己 Mac 上**的 AI 对话中枢：

```
┌───────────────────────────┐        HTTP (127.0.0.1:8000)        ┌──────────────────────────────┐
│  macOS 客户端 (SwiftUI)    │ ──────────────────────────────────▶ │  FastAPI 后端 (main.py)       │
│  AIChatApp.app            │ ◀────────────────────────────────── │  · 101 个运维工具             │
│  · 登录 / 对话 / 设置       │      POST /api/chat_async + 轮询      │  · SQLite 会话持久化          │
│  · 拉起来/停止本地后端       │                                     │  · JWT 鉴权 / Google / GitHub │
└───────────────────────────┘                                     └───────────┬──────────────────┘
                                                                              │ function calling
                                          ┌────────────────┬────────────────┬──────────────────┬────────────────┐
                                          ▼                ▼                ▼                  ▼
                                    Azure (21)       Meraki (45)      Nexus Dash (31)   DeepSeek/Kimi/OpenAI
```

- **对话即运维**：模型可以通过 function calling 直接查 Azure 订阅/资源/指标、查 Meraki 网络设备与防火墙策略、
  查 Cisco Nexus Dashboard 集群健康与 fabric/交换机清单（官方 Infra API + Manage API，只读）。
- **密钥不出本机**：API Key 只存在本地 `.env` 与 macOS Keychain 中，客户端只与 `127.0.0.1` 通信。
- **原生 Apple Silicon**：客户端与后端打包产物均为 arm64（不使用 Rosetta）。
- **界面语言 / 字号**：设置页可切换 中文 / English / Norsk (bokmål)（立即生效，不用重启）；
  字号用 ⌘+ / ⌘− / ⌘0 调整，分区标题跟着一起缩放。

---

## 下载安装（普通用户）

不想自己编译的话，直接下载发布包：

👉 **[最新版 Releases](https://github.com/gtsdrt/gtsdrt_ai_hub_macos/releases/latest)** — 下载 `AIChatApp-<版本>.dmg`，把 App 拖进「应用程序」即可。

- 系统要求：macOS 13+，**Apple Silicon（M 系列）**；Intel Mac 无法运行（本项目只发布 arm64）
- 内嵌后端自带 101 个工具，**不需要**另行安装 Python 或依赖
- **首次打开会被 Gatekeeper 拦截**（发布包是 ad-hoc 签名、未经 Apple 公证）：右键（或 Control + 点击）App → **打开** → 弹窗里再点一次「打开」；或执行
  `xattr -dr com.apple.quarantine /Applications/AIChatApp.app`
- 建议用 Release 页面公布的 SHA-256 核对下载文件

想从源码运行、或自己打包 → 见下方「快速开始」。

---

## 目录结构

```
.
├── main.py                       # FastAPI 后端（唯一正式实现）
├── tools/                        # 工具集（AI 可调用）
│   ├── __init__.py               #   注册表：default_schemas() / execute_tool() / health()
│   ├── azure_tools.py            #   21 个 Azure 工具 + 凭据探测
│   ├── meraki_tools.py           #   45 个 Meraki 工具（直连 Dashboard REST API）
│   ├── nexus_dashboard_tools.py  #   31 个 Nexus Dashboard 工具（官方 Infra + Manage API）
│   ├── container_tools.py        #    3 个 Serverless 容器工具（多容器注册表，热加载）
│   └── ai_tools.py               #   1 个 AI 工具（DeepSeek 余额查询）
├── function_app.py               # 【历史版本】原 Azure Functions 实现，仅作参考，不再被引用
├── requirements-fastapi.txt      # 后端依赖
├── .env.example                  # 环境变量模板（复制成 .env 后填写）
├── backend.spec                  # PyInstaller 打包配置（默认 onefile，PYI_ONEDIR=1 出目录版）
├── scripts/                      # arm64 原生执行包装 + 打包产物端到端验证
├── test_*.py                     # 单元测试（unittest，可直接运行）
└── AIChatApp/                    # macOS SwiftUI 客户端（Xcode 工程）
    ├── Sources/                  #   Views / ViewModels / Services / Models
    ├── scripts/build_dmg.sh      #   一键出 DMG（archive → 签名（可选）→ DMG）
    └── README.md                 #   客户端详细文档
```

---

## 快速开始

### 1. 起后端

```bash
git clone https://github.com/gtsdrt/gtsdrt_ai_hub_macos.git
cd gtsdrt_ai_hub_macos

python3 -m venv .venv
.venv/bin/pip install -r requirements-fastapi.txt

cp .env.example .env        # 至少填一个 AI Provider 的 Key；不填也能进 mock 模式
.venv/bin/python main.py    # 默认 http://127.0.0.1:8000
```

打开 <http://127.0.0.1:8000/docs> 可以直接调所有接口。不配置任何 Key 时后端返回 mock 回复，方便先联调前端。

### 2. 跑起来做一次冒烟验证

```bash
curl -s http://127.0.0.1:8000/api/health | python3 -m json.tool

TOKEN=$(curl -s -X POST http://127.0.0.1:8000/api/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"password123"}' | python3 -c 'import json,sys;print(json.load(sys.stdin)["token"])')

curl -s http://127.0.0.1:8000/api/chat -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"你好","enable_tools":true}' | python3 -m json.tool
```

对话请求里 `prompt`（单轮）与 `messages`（多轮）二选一；`enable_tools: true` 才会把工具挂载给模型。

### 3. 打开 macOS 客户端

```bash
open AIChatApp/AIChatApp.xcodeproj
# 或纯命令行：
xcodebuild -project AIChatApp/AIChatApp.xcodeproj -scheme AIChatApp \
           -configuration Debug -derivedDataPath .xcbuild build
open .xcbuild/Build/Products/Debug/AIChatApp.app
```

首次启动会：自动探测项目目录与 `.venv/bin/python` → 拉起 `main.py` → 用 `/api/health` 做健康检查。
登录默认 `admin` / `password123`。

### 4. 跑测试

```bash
.venv/bin/python test_storage.py         # SQLite 持久化
.venv/bin/python test_azure_tools.py     # Azure 工具（mock）
.venv/bin/python test_meraki_tools.py    # Meraki 工具（mock）
.venv/bin/python test_nexus_dashboard_tools.py  # Nexus Dashboard 工具（mock）
.venv/bin/python test_google_auth.py     # Google OAuth / PKCE
.venv/bin/python test_github_auth.py     # GitHub Device Flow
```

### 5. 打包

```bash
# 后端 → 单文件可执行（arm64）
.venv/bin/pyinstaller backend.spec --clean     # 产物 dist/backend_server
PYI_ONEDIR=1 .venv/bin/pyinstaller backend.spec --clean   # 目录版，冷启动快约 20 倍

# 客户端 → DMG（产物 AIChatApp/AIChatApp-<版本>.dmg）
AIChatApp/scripts/build_dmg.sh
```

打包脚本会硬校验 arm64 架构；在 Rosetta 终端里会直接拒绝执行。中间的构建产物（`dist/`、`build/`、`*.dmg` 等）已在 `.gitignore` 中排除，不入库。

---

## 工具集（101 个）

| 分组 | 数量 | 能力 |
|---|---|---|
| **Azure** | 21 | 订阅/资源组、VM 与 CPU 指标、VNet/子网/NSG 检查、Storage/Web/SQL/KeyVault/AKS/ACI 清点、KQL `query_resources`、监控指标与指标定义 |
| **Meraki** | 45 | 组织/网络/设备、VPN 状态与站点、设备上联、Appliance（VLAN/端口/静态路由/防火墙 L3·L7/1:1 与 1:Many NAT/端口转发/入侵检测/内容过滤/流量整形）、交换机端口与镜像、无线 SSID/RF Profile |
| **Nexus Dashboard** | 31 | 官方 Infra API（集群/节点/健康/容量/硬件/许可/租户/远端存储/审计/集成/通用设置）+ Manage API（fabric 汇总与状态、全局与按 fabric 的交换机、接口汇总、网络/VRF/租户、邻居交换机）；另有 `nexus_overview` 一次聚合集群信息 + 健康 + fabric + 交换机汇总。**全部只读** |
| **Serverless 容器** | 3 | 多容器注册表（见下方「容器工具怎么配」）：`container_list_endpoints` 列出已注册容器、`container_list_tasks` 列出某容器的任务与可调用性、`container_run_task` 执行白名单任务。容器里能执行任意 playbook 的 `/run-ansible` **不开放**给模型 |
| **AI** | 1 | DeepSeek 账户余额 |

只有请求里带 `"enable_tools": true` 时才会把全部 schema 挂载给模型（默认 `false`，可用环境变量 `TOOLS_ENABLED_BY_DEFAULT` 改成默认开启）；工具循环最多 `MAX_TOOL_ITERATIONS`（默认 10）轮。

### Nexus Dashboard 工具怎么配

1. 设置 `ND_BASE_URL`（例如 `https://nd.example.com`，只填主机，不要带 `/api/v1/...`）。
2. 二选一认证（同时填时优先 API Key）：
   - **API Key**：登录 Nexus Dashboard → 右上角用户名 → **Manage API keys** → Add API key，把 key 填到 `ND_API_KEY`，
     账号填 `ND_USERNAME`（API Key 只对 local 账号有效）；
   - **用户名密码**：填 `ND_USERNAME` + `ND_PASSWORD`，后端会自动 `POST /api/v1/infra/login` 换 token 并在进程内缓存续期，
     登录域用 `ND_LOGIN_DOMAIN`（默认 `local`）。
3. 自签证书的集群保持 `ND_VERIFY_TLS=false`（默认值）；换成受信任证书后再设为 `true`。
4. 在 App 的「设置 → Nexus Dashboard 工具凭据」里也可以直接填这些值，保存后点「测试连接」会调用 `/api/v1/infra/about` 验证。

> 端点路径按 Cisco 官方 OpenAPI 规范核对（Infra `https://<nd>/api/v1/infra`、Manage `https://<nd>/api/v1/manage`，4.3.1 版规范），
> 且只注册了只读 `GET`，不包含任何创建/修改/删除/升级操作。

### 容器工具怎么配（多容器注册表）

容器（例如 [gtsdrt/Azure_ACR](https://github.com/gtsdrt/Azure_ACR)）的代码在 GitHub，
由 GitHub Actions 构建镜像推到 ACR，再以 Serverless 容器（Azure Container Apps）运行。
App 只是调用方，所以构建/部署跟本机没关系——**改代码 push 到 GitHub 就自动重新部署**。

**注册表文件**（唯一配置来源，后端每次调用都重读 → 改完**立即生效，不用重启后端**）：

```
~/Library/Application Support/AIChatApp/containers.json      # 可用 AICHAT_CONTAINERS_FILE 改路径
```

```json
{
  "containers": [
    {
      "name": "ansible",
      "url": "https://ansible-executor.xxx.norwayeast.azurecontainerapps.io",
      "api_key": "...",                    // 或 "api_key_env": "ANSIBLE_EXECUTOR_API_KEY"
      "mode": "auto",                      // auto（默认）/ list / all
      "allowed_tasks": ["meraki_get_switch_ports"],
      "timeout": 300,
      "description": "执行 Ansible playbook 的容器"
    }
  ]
}
```

| 字段 | 说明 |
|---|---|
| `name` | 容器名，模型用它指定容器（也是 `container_list_tasks` / `container_run_task` 的 `container` 参数） |
| `url` | 容器 FQDN（Azure 门户 → Container App → 概述 → 应用程序 URL），只填主机、不要带 `/tasks` |
| `api_key` / `api_key_env` | 容器要求的 `X-API-Key`；`api_key_env` 指向环境变量，密钥就不用落在文件里 |
| `mode` | `auto`（默认）/ `list`（只信 `allowed_tasks`）/ `all`（不限制，危险） |
| `allowed_tasks` | 白名单；写 `"*"` 等价于 `mode: all` |
| `timeout` | 单次请求超时秒数，默认 300（ansible 任务可能跑几分钟） |

**白名单判定（mode=auto，默认）**：`allowed_tasks` 里列出的放行 → 容器在 `GET /tasks` 里标了
`read_only: true` 的任务自动放行 → 再回退到内置只读名单（`meraki_get_switch_ports` /
`meraki_get_switch_port_statuses` / `meraki_get_firewall_rules`）。写操作默认一律拦住。

**加一个新容器**：往 `containers.json` 里加一条 → 立刻可用（不用改代码、不用重启后端）。
契约相同的容器（同样是 `/tasks` + `/run-task` + `X-API-Key`）直接复用；契约不同的容器
需要新增工具模块（本模块只支持 `protocol: "azure-acr"`）。

兼容旧写法：只配 `ANSIBLE_EXECUTOR_URL` + `ANSIBLE_EXECUTOR_API_KEY` 也可以，
后端会把它当成一个名为 `ansible` 的条目；App 首次打开设置页时会自动迁移成注册表条目。

其它：
- App「设置 → Serverless 容器」里可以直接编辑注册表 JSON + 「测试连接」（后端 target=`container`，用 `GET /tasks` 做只读探测）。
- 容器处于 `Stopped` 时调用会明确提示（`az containerapp start -n ansible-executor -g ai-api`）；
  想让它在无请求时零成本，把 `minReplicas` 改成 `0`。

> 只注册了 3 个工具：`container_list_endpoints` / `container_list_tasks` / `container_run_task`
> （对应 `GET /tasks`、`POST /run-task`）。容器里那个能执行**任意** playbook 的 `POST /run-ansible`
> 故意不暴露——它等价于容器内任意代码执行。

---

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/login` | 本地管理员登录，签发 JWT |
| `POST` | `/api/auth/google/start` | 启动 Google OAuth（Authorization Code + PKCE） |
| `GET` | `/api/auth/google/callback` | Google 回调（浏览器），签发 JWT |
| `GET` | `/api/auth/google/status` | 轮询登录状态 |
| `POST` | `/api/auth/github/start` | 启动 GitHub Device Flow（返回 user_code） |
| `GET` | `/api/auth/github/status` | 轮询设备授权状态 |
| `GET` | `/api/health` | 健康检查：Python/架构、AI 配置、存储后端、Azure 凭据探测 |
| `POST` | `/api/test_connection` | 测试 `azure` / `meraki` / `nexus_dashboard` / `container` / `deepseek` 连通性 |
| `POST` | `/api/chat` | 同步对话（长请求，最大 360s） |
| `POST` | `/api/chat_async` | 异步对话，立即返回 `instance_id`（202） |
| `GET` | `/api/ai_task_status` | 轮询异步任务结果 |
| `GET` | `/api/chat_history` | 读取单个会话历史（含工具调用日志） |
| `GET` | `/api/chat_conversations` | 会话列表 |
| `DELETE` | `/api/chat_conversation` | 删除会话 |

除 `/api/login`、`/api/auth/*`、`/api/health` 外均需 `Authorization: Bearer <JWT>`。

---

## 配置（`.env`）

完整清单见 [`.env.example`](.env.example)，常用的几组：

| 变量 | 说明 |
|---|---|
| `DEEPSEEK_API_KEY` / `KIMI_API_KEY` | AI Provider Key，都为空则进 mock 模式 |
| `DEFAULT_AI_PROVIDER` | `deepseek` 或 `kimi` |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_AUTH_MODE` | 原生 OpenAI 或任意兼容端点（含 Azure Foundry，`auto` 会自动带 `api-key` 头） |
| `JWT_SECRET` / `ADMIN_USERNAME` / `ADMIN_PASSWORD` | **生产环境务必改掉默认值** |
| `GOOGLE_CLIENT_ID` / `GOOGLE_ALLOWED_EMAILS` / `GOOGLE_ALLOWED_DOMAINS` | Google 登录，建议至少配一个白名单 |
| `GITHUB_CLIENT_ID` / `GITHUB_ALLOWED_LOGINS` / `GITHUB_ALLOWED_EMAILS` | GitHub 登录，同样建议配白名单 |
| `AZURE_TENANT_ID` / `AZURE_CLIENT_ID` / `AZURE_CLIENT_SECRET` / `AZURE_SUBSCRIPTION_ID` | Azure 工具所需服务主体 |
| `MERAKI_API_KEY` | Meraki Dashboard API Key（OAuth token 时另设 `MERAKI_AUTH_HEADER=bearer`） |
| `ND_BASE_URL` / `ND_USERNAME` | Nexus Dashboard 集群地址与账号（如 `https://nd.example.com`） |
| `ND_API_KEY` **或** `ND_PASSWORD` + `ND_LOGIN_DOMAIN` | 两种官方认证方式二选一：API Key（`X-Nd-Username` + `X-Nd-Apikey`）或用户名密码登录换 token（默认域 `local`） |
| `ND_VERIFY_TLS` | 默认 `false`（Nexus Dashboard 出厂多为自签证书，等同官方示例的 `--insecure`） |
| `HOST` / `PORT` / `API_PREFIX` / `LOG_LEVEL` | 服务监听与日志 |
| `AICHAT_DB_PATH` | 会话库位置，默认 `~/Library/Application Support/AIChatApp/aichat.db` |
| `AI_MOCK_MODE` / `MAX_TOOL_ITERATIONS` / `TASK_TTL_HOURS` | 行为调优 |
| `TOOLS_ENABLED_BY_DEFAULT` | 设为 `true` 时所有对话默认挂载 101 个工具（默认 `false`，按需在请求里开） |

系统环境变量优先于 `.env`（已存在的变量不会被覆盖）。

---

## 平台要求

| 项目 | 要求 |
|---|---|
| macOS | 13.0 或更高（客户端 `MACOSX_DEPLOYMENT_TARGET = 13.0`） |
| CPU | Apple Silicon（arm64）。客户端与打包脚本都硬性拒绝 Rosetta，x86_64 需自行改配置 |
| Python | 3.11+（开发环境实测 3.14） |
| Xcode | 完整版 Xcode（构建客户端需要，Command Line Tools 不够） |

---

## 安全说明

- `.env`、`*.db`、`*.pem`、构建产物与 DMG 均已加入 `.gitignore`，**不会入库**。
- 历史版本 `function_app.py` 在入库前已做脱敏：租户/订阅 GUID、Key Vault 与 Azure OpenAI 端点、内部网络命名均替换为环境变量或占位符（`TENANT_SECONDARY_ID`、`SUBSCRIPTION_SECONDARY_ID`、`KEY_VAULT_URL`）。
- 默认的 `JWT_SECRET` / `ADMIN_PASSWORD` 仅供本机开发，部署到任何可被外部访问的环境前必须更换。
- 后端默认监听 `0.0.0.0:8000`。只想本机使用的话请设 `HOST=127.0.0.1`，并确保不要暴露到公网。

---

## 许可证

[MIT License](LICENSE) © 2026 gtsdrt

你可以自由使用、修改、分发（包括商用）。软件按「原样」提供，不附带任何担保；
使用本工具对你的 Azure / Meraki / Nexus Dashboard 环境做任何操作，风险由使用者自行承担。
