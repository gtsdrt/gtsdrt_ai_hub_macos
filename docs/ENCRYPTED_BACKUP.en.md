# AIChatApp Encrypted Backup and Restore

[中文版](ENCRYPTED_BACKUP.md) · [Deployment and updates](AUTO_UPDATE.en.md)

Implemented on 2026-10-03. Open Settings → Backup and Restore. This version provides manual encrypted backups, password verification and preview, selective restore, and rollback on restore failures.

Released in [AIChatApp 0.3.8](https://github.com/gtsdrt/gtsdrt_ai_hub_macos/releases/tag/v0.3.8-build.1), build `10.8.1`. Existing installations can upgrade through “Check for Updates…” in the app menu.

## Creating a backup

1. Save your settings first, then choose “Create Encrypted Backup…”. Backups read saved preferences and Keychain entries; unsaved field drafts are excluded.
2. Enter and confirm a separate strong password: at least 12 characters, at most 1024 UTF-8 bytes. Unicode, spaces, and case are preserved exactly.
3. Optionally include Keychain passwords/API keys and backend logs. Both options are off by default.
4. Choose a destination for the `.aichatbackup` file. Local folders, iCloud Drive, or a mounted NAS can be used; the app does not upload backups itself.
5. Keep the password safely. This version does not remember it, recover lost passwords, or schedule backups.

## Included data

| Data | Behavior |
| --- | --- |
| Chats and tasks | SQLite Online Backup API creates a consistent snapshot, including uncheckpointed WAL commits; settings can still be backed up when no database exists |
| Preferences | Allowlisted AI/tool/OAuth settings, language, font size, tool switches, container registry draft, and names of migrated credential variables |
| Container file | The actual `containers.json`, restored together with preferences |
| Credentials (optional) | Only known app password/API key/JWT/container/migrated entries under Keychain service `com.example.AIChatApp` |
| Logs (optional) | Contents read from `backend.log`; a running backend may continue appending |

Development/Python paths, window state, Sparkle preferences, `.env`, Azure CLI caches, update signing private keys, PID files, network caches, and installation/build artifacts are excluded. Development paths on another Mac retain that Mac's existing values. Existing history retention rules still apply: backups cannot recover messages/tasks already truncated or deleted.

Registries containing legacy plaintext `api_key` fields are rejected. Save the registry in Settings to complete Keychain migration, then create the backup again. Do not edit a backup to bypass validation.

## Restoring a backup

1. Choose “Restore from Backup…”, select a file, and enter its original password.
2. Choose “Verify and Preview”. This does not modify existing data. Preview appears only after authentication, format, preference-type, and database checks pass.
3. Check the date, source version, conversation/task counts, and select components: chats/tasks, preferences/containers, credentials, and logs. Unselected components are not restored; a completed restore still signs out and refreshes the interface.
4. Confirm the overwrite. Active requests are interrupted. The app stops its owned backend and waits for the process to exit. Externally started backends must be stopped manually; remote backends and memory-only storage are unsupported.
5. Before changing selected components, an encrypted pre-restore backup is saved at:

   ```text
   ~/Library/Application Support/AIChatApp/Backups/BeforeRestore-<UUID>.aichatbackup
   ```

   **It uses the same password entered for this restore.** It contains the previous data needed to undo the restore. Previous credentials/logs are included only when their corresponding restore options are selected.
6. Chats/tasks replace current records; selected preferences/container configuration are replaced; credentials overwrite matching names while unrelated entries remain. Restored login tokens are not a guarantee of access: sign in again afterward.
7. Settings reload, and the backend restarts according to its previous running state/automatic-start preference. Task records are restored; interrupted jobs do not resume automatically.

“Show Pre-Restore Backup” opens its location. To undo a restore, select that file through the same restore workflow and use the same password.

## Encryption and file format

- System CommonCrypto PBKDF2-HMAC-SHA256 derives a 256-bit key from the password using 600,000 iterations and a fresh random 16-byte salt per backup.
- Apple CryptoKit AES-256-GCM generates a fresh random nonce and an authentication tag.
- Binary layout: `AICHATBK1` (9 bytes), big-endian iteration count (4 bytes), salt (16 bytes), and GCM combined data (nonce + ciphertext + tag).
- The header is additional authenticated data. Changes to the salt, KDF parameters, or ciphertext fail authentication.
- Encrypted JSON contains the format version, date, app version, SQLite snapshot, preferences plist, container JSON, optional credentials, and optional logs. Credentials enter the ciphertext in memory; no plaintext credential file is created.
- Neither the plaintext password nor the decryption key is stored in the file. Reader limits bound file size and KDF work.
- Maximum backup file size is 128 MiB. JSON/base64 adds overhead, so the raw database cannot consume the entire limit.

Temporary database snapshots and validation copies briefly exist on disk in directories with mode `700`, files with mode `600`, and are removed after each operation. Backup/recovery files and restored databases are written with mode `600`. The live database remains ordinary SQLite; this feature encrypts backup files.

## Failures and recovery guarantees

- Incorrect passwords, tampering, unsupported formats, invalid preferences, unrelated databases, or databases containing triggers/views are rejected.
- Denied Keychain access causes failure; missing credentials are not silently reported as a successful export.
- Write failures roll back selected components and preserve the encrypted pre-restore file. If rollback also fails, its location is shown and the backend remains stopped for another recovery attempt.
- This is application-level rollback, not an OS transaction spanning SQLite, preferences, and Keychain. Forced termination or power loss may require restoring the pre-restore file.
- The app does not confirm cloud synchronization or manage backup retention. Verify the password/preview before deleting backups and keep an off-device copy.

## Developer validation

```bash
xcrun swiftc -parse-as-library \
  AIChatApp/Sources/Services/BackupArchive.swift \
  AIChatApp/Sources/Services/BackupRepository.swift \
  scripts/test_encrypted_backup.swift \
  -o /tmp/aichat-backup-tests
/tmp/aichat-backup-tests
```

Tests use temporary databases, an isolated UserDefaults suite, and a fake credential store. They do not read/write real chats or Keychain entries. GitHub Actions runs the same tests before every release. An independent Python PBKDF2/AES-GCM implementation also verified decryption of the Swift-generated test file.

### Release 0.3.8 validation record

- All 40 isolated checks passed locally and in GitHub Actions, covering WAL snapshots, wrong passwords, tampering, size/type limits, selective restore and rollback after partial write failures.
- The full local Xcode build and [production release build](https://github.com/gtsdrt/gtsdrt_ai_hub_macos/actions/runs/37128615323) passed.
- The actual SwiftUI backup views were rendered with fake services at 115%/200% font scale to check create/restore text and field layout. No restore was performed against real user data.
- After downloading the published ZIP, version/build, update URL, archive length, Ed25519 update signature and deep app code signature were verified. `latest` points to `v0.3.8-build.1`. This does not confirm Apple notarization; signing retains the ad-hoc setup described in the deployment guide.

References: [SQLite Online Backup](https://www.sqlite.org/backup.html), [Apple AES.GCM](https://developer.apple.com/documentation/cryptokit/aes/gcm), [OWASP PBKDF2 parameters](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html#pbkdf2).
