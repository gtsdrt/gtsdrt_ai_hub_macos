# AIChatApp 加密备份与恢复

[English](ENCRYPTED_BACKUP.en.md) · [部署与更新](AUTO_UPDATE.md)

实现日期：2026-10-03。入口为「设置 → 备份与恢复」。本版本提供手动加密备份、密码验证与预览、按范围恢复，以及恢复失败时的回滚。

## 创建备份

1. 先在设置页保存配置，再选择「创建加密备份…」。备份读取已保存的设置和钥匙串条目，不保存尚未提交的输入草稿。
2. 输入独立的强密码，并再次确认。至少 12 个字符，最多 1024 个 UTF-8 字节；密码中的中文、空格和大小写均按原样处理。
3. 按需勾选「包含钥匙串密码和 API Key」「包含后端日志」，这两项默认关闭。
4. 选择保存位置，生成 `.aichatbackup` 文件。文件可放在本机、iCloud Drive 或已挂载的 NAS 目录；应用本身不上传备份。
5. 妥善保存密码；本版本不记住备份密码，也不提供密码找回或定时备份。

## 数据范围

| 数据 | 行为 |
| --- | --- |
| 聊天及任务 | SQLite Online Backup API 生成一致快照，包含尚未 checkpoint 的 WAL 数据；没有数据库时仍可备份设置 |
| 设置 | 白名单中的 AI/工具/OAuth 配置、语言、字号、工具开关、容器注册表草稿与已迁移凭据的变量名 |
| 容器文件 | 实际使用的 `containers.json`，与设置一起恢复 |
| 凭据（可选） | 仅 Keychain service `com.example.AIChatApp` 中本 App 已知的密码、API key、JWT 和容器/迁移凭据条目 |
| 日志（可选） | `backend.log` 的读取时内容；运行中的日志可继续追加 |

开发目录及 Python 路径、窗口状态、Sparkle 更新状态、`.env`、Azure CLI 缓存、更新签名私钥、PID、网络缓存和安装/编译产物不属于备份范围。另一台 Mac 上的开发目录保持本机原值。历史记录仍遵循后端的保留规则：备份无法补回已经截断或清理的消息/任务。

若注册表中仍存在旧版明文 `api_key`，备份校验会拒绝该配置。先在设置中保存注册表，让 App 完成向钥匙串的迁移，再重新备份；不要通过直接编辑备份绕过校验。

## 从备份恢复

1. 选择「从备份恢复…」并打开文件，输入原始备份密码。
2. 选择「验证并预览」。此步骤不会修改现有数据。只有密码、认证标签、数据格式、设置类型和数据库校验均通过后才显示预览。
3. 核对时间、来源版本、会话及任务数量，选择恢复范围：聊天/任务、设置/容器、凭据、日志。未选择的组件不会被恢复；恢复完成仍会退出当前登录并重新加载界面。
4. 确认后，正在执行的请求被中断，应用停止自己启动的后端，并等待进程实际退出。由终端等外部启动的后端必须先自行停止；远程后端、内存存储模式不支持此操作。
5. 在修改任何选中组件前，应用生成恢复前的加密备份，保存在：

   ```text
   ~/Library/Application Support/AIChatApp/Backups/BeforeRestore-<UUID>.aichatbackup
   ```

   **该文件使用本次恢复输入的同一备份密码**，包含用于撤销本次恢复的原数据；只有选择恢复凭据/日志时，才会把原凭据/日志放入这份恢复前备份。
6. 数据库替换当前记录；设置和容器配置替换选中内容；凭据覆盖同名条目，不删除不相关条目。恢复前的登录 token 不会成为可靠的登录保证，恢复后需重新登录。
7. 完成后，应用重新加载设置，并按之前运行状态/自动启动设置重新启动后端。任务记录可以恢复，已中断的任务不会自动继续执行。

设置页的「显示恢复前备份」可打开相应文件的位置。撤销恢复时，按同样流程打开该恢复前文件，并使用同一密码。

## 加密及文件格式

- 使用系统 CommonCrypto 的 PBKDF2-HMAC-SHA256，将密码派生为 256 位密钥：600,000 次迭代，每份备份新生成 16 字节随机 salt。
- 使用 Apple CryptoKit 的 AES-256-GCM。每份加密生成新的随机 nonce，并附带认证标签。
- 二进制文件布局：`AICHATBK1`（9 字节）、大端迭代次数（4 字节）、salt（16 字节）、GCM combined 数据（nonce + 密文 + 标签）。
- 文件头作为 GCM 的附加认证数据。修改 salt、迭代参数或密文会导致验证失败。
- 被加密的 JSON payload 包含格式版本、时间、App 版本、SQLite 快照、偏好设置 plist、容器 JSON、可选凭据与日志。凭据只在内存中进入密文，不生成明文凭据文件。
- 文件不含明文备份密码或解密密钥。读取端限制文件大小及 KDF 迭代次数，避免无界内存分配/计算。
- 最大备份文件为 128 MiB。数据库经过 JSON/base64 编码后会增大，不能把 128 MiB 全部用于原始数据库。

临时数据库快照和校验副本会短暂落盘：目录权限 `700`、文件权限 `600`，操作结束时删除。备份、恢复前文件和恢复后的数据库按 `600` 写入。现有聊天数据库本身仍是普通 SQLite，本功能仅加密备份文件。

## 错误与恢复保障

- 密码错误、文件篡改、格式不支持、非法设置、非 App 数据库或带触发器/视图的数据库会被拒绝。
- 钥匙串读写被拒绝时，操作失败，不会把缺失的凭据默认为成功导出。
- 写入失败时，对选中组件逐一回滚，并保留加密恢复前文件；若回滚也失败，显示该文件路径并保持后端停止，供用户再次恢复。
- 这是应用层回滚，不是跨 SQLite、偏好设置和 Keychain 的操作系统事务。进程被强制结束或断电时仍应使用恢复前文件恢复。
- App 不负责云目录的上传成功或历史版本保留。删除备份前应先验证密码及预览，并妥善保存至少一份离机副本。

## 开发验证

```bash
xcrun swiftc -parse-as-library \
  AIChatApp/Sources/Services/BackupArchive.swift \
  AIChatApp/Sources/Services/BackupRepository.swift \
  scripts/test_encrypted_backup.swift \
  -o /tmp/aichat-backup-tests
/tmp/aichat-backup-tests
```

测试只使用临时数据库、独立 UserDefaults suite 和虚构凭据存储，不读写真实用户聊天或钥匙串。GitHub Actions 在每次发布前运行同一套测试。另以独立 Python PBKDF2/AES-GCM 实现验证 Swift 生成的测试文件可正确解密。

参考：[SQLite Online Backup](https://www.sqlite.org/backup.html)、[Apple AES.GCM](https://developer.apple.com/documentation/cryptokit/aes/gcm)、[OWASP PBKDF2 参数](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html#pbkdf2)。
