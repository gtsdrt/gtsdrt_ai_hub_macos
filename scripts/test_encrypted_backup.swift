import Foundation
import SQLite3

// Standalone tests link the actual backup implementation, with isolated data and
// a fake credential store. Never access this user's database, defaults or Keychain.
func L(_ text: String) -> String { text }
func L(_ text: String, _ arguments: String...) -> String {
    arguments.enumerated().reduce(text) { $0.replacingOccurrences(of: "{\($1.offset)}", with: $1.element) }
}

final class FakeCredentials: BackupCredentialStore {
    var values: [String: String] = ["openai_api_key": "original-dummy-key", "container_api_key_demo": "dummy-container-key"]
    var failNextWrite = false
    func exportBackupCredentials() throws -> [String: String] { values }
    func applyBackupCredentials(_ entries: [String: String], removing: Set<String>) throws {
        for key in removing { values.removeValue(forKey: key) }
        for (key, value) in entries { values[key] = value }
        if failNextWrite { failNextWrite = false; throw BackupError.credentials }
    }
}

@main
struct BackupTests {
    static func main() throws {
        var checks = 0
        func check(_ condition: Bool, _ name: String) {
            precondition(condition, "FAIL: \(name)"); checks += 1
        }
        func rejects(_ name: String, _ operation: () throws -> Void) {
            do { try operation(); fatalError("FAIL: accepted \(name)") } catch { checks += 1 }
        }
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("AIChatBackupTests-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false)
        defer { try? FileManager.default.removeItem(at: directory) }
        let suite = "AIChatBackupTests-\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }
        let credentials = FakeCredentials()
        let repository = BackupRepository(databaseURL: directory.appendingPathComponent("live.db"),
            registryURL: directory.appendingPathComponent("containers.json"),
            logURL: directory.appendingPathComponent("backend.log"),
            recoveryDirectory: directory.appendingPathComponent("Backups"),
            defaults: defaults, credentialStore: credentials)
        defaults.set("original-model", forKey: "openaiModel")
        defaults.set(1.15, forKey: "ui.fontScale")
        defaults.set(true, forKey: "autoStartBackend")
        defaults.set("/keep/this/device/path", forKey: "projectDirectory")
        let registry = Data("{\"containers\":[{\"name\":\"demo\",\"url\":\"https://example.invalid\"}]}".utf8)
        try BackupArchive.writePrivate(registry, to: repository.registryURL)
        try BackupArchive.writePrivate(Data("original-log".utf8), to: repository.logURL)
        var live: OpaquePointer?
        guard sqlite3_open(repository.databaseURL.path, &live) == SQLITE_OK else { fatalError("open test database") }
        defer { sqlite3_close(live) }
        func sql(_ query: String) {
            precondition(sqlite3_exec(live, query, nil, nil, nil) == SQLITE_OK, "test SQL failed")
        }
        sql("PRAGMA journal_mode=WAL; PRAGMA wal_autocheckpoint=0;")
        sql("CREATE TABLE conversations(id TEXT PRIMARY KEY,title TEXT,messages TEXT,updated_at TEXT,message_count INTEGER);")
        sql("CREATE TABLE tasks(instance_id TEXT PRIMARY KEY,status TEXT,result TEXT,error TEXT,created_at TEXT,updated_at TEXT);")
        sql("INSERT INTO conversations VALUES('one','private-title','[]','today',1);")
        sql("INSERT INTO tasks VALUES('task','completed','private-result',NULL,'today','today');")
        let payload = try repository.capture(includeCredentials: true, includeLogs: true, appVersion: "test")
        let summary = try BackupPolicy.validate(payload)
        check(summary.conversations == 1 && summary.tasks == 1, "snapshot includes uncheckpointed WAL commits")
        let password = "test-password-中文-12345"
        let encrypted = try BackupArchive.seal(payload, password: password)
        let second = try BackupArchive.seal(payload, password: password)
        check(encrypted != second, "fresh salt and nonce for every backup")
        check(encrypted.range(of: Data("original-dummy-key".utf8)) == nil, "no plaintext credentials")
        check(encrypted.range(of: Data("private-title".utf8)) == nil, "no plaintext chat metadata")
        let decoded = try BackupArchive.open(encrypted, password: password)
        check(decoded.database == payload.database && decoded.credentials == payload.credentials, "Unicode password round trip")
        rejects("wrong password") { _ = try BackupArchive.open(encrypted, password: "wrong-password") }
        rejects("weak export password") { _ = try BackupArchive.seal(payload, password: "short") }
        var tampered = encrypted; tampered[tampered.count - 1] ^= 1
        rejects("tampered tag") { _ = try BackupArchive.open(tampered, password: password) }
        tampered = encrypted; tampered[29 + 12] ^= 1
        rejects("tampered ciphertext") { _ = try BackupArchive.open(tampered, password: password) }
        tampered = encrypted; tampered[28] ^= 1
        rejects("tampered salt/header") { _ = try BackupArchive.open(tampered, password: password) }
        tampered = encrypted; tampered[9] = 255
        rejects("unbounded KDF work") { _ = try BackupArchive.open(tampered, password: password) }
        rejects("truncated file") { _ = try BackupArchive.open(encrypted.prefix(30), password: password) }
        rejects("wrong file type") { _ = try BackupArchive.open(Data(repeating: 0, count: 100), password: password) }
        let large = directory.appendingPathComponent("oversized.aichatbackup")
        try BackupArchive.writePrivate(Data(), to: large)
        let largeHandle = try FileHandle(forWritingTo: large)
        try largeHandle.truncate(atOffset: UInt64(BackupArchive.maximumSize + 1))
        try largeHandle.close()
        rejects("oversized file before allocation") { _ = try BackupArchive.read(large) }
        var prefs = try BackupPolicy.preferences(payload.preferences)
        check(prefs["projectDirectory"] == nil, "machine paths excluded from portable preferences")
        prefs["evilPreference"] = "unexpected"
        let invalidPrefs = try PropertyListSerialization.data(fromPropertyList: prefs, format: .binary, options: 0)
        rejects("unknown preference") { _ = try BackupPolicy.preferences(invalidPrefs) }
        prefs.removeValue(forKey: "evilPreference"); prefs["autoStartBackend"] = "true"
        rejects("wrong preference type") {
            _ = try BackupPolicy.preferences(PropertyListSerialization.data(fromPropertyList: prefs, format: .binary, options: 0))
        }
        rejects("non-SQLite database") { _ = try BackupDatabase.validate(Data(repeating: 0, count: 200)) }
        sql("CREATE TRIGGER unsafe AFTER INSERT ON tasks BEGIN DELETE FROM conversations; END;")
        rejects("executable database trigger") { _ = try BackupDatabase.snapshot(repository.databaseURL) }
        sql("DROP TRIGGER unsafe;")
        let noSecrets = try repository.capture(includeCredentials: false, includeLogs: false, appVersion: "test")
        check(noSecrets.credentials == nil && noSecrets.log == nil, "credential/log export opt out")
        check(!BackupPolicy.allowedCredential("env_secret_sparkle_private_key"), "update signing key excluded")
        // Stop the writer before exercising restore and rollback, just as the app does.
        sqlite3_close(live); live = nil
        defaults.set("modified-model", forKey: "openaiModel")
        credentials.values["openai_api_key"] = "modified-key"
        try BackupArchive.writePrivate(Data("modified-log".utf8), to: repository.logURL)
        let restore = BackupRepository.Selection(database: true, settings: true, credentials: true, logs: true)
        let recovery = try repository.restore(decoded, selection: restore, password: password, appVersion: "test")
        check(defaults.string(forKey: "openaiModel") == "original-model", "preferences restored")
        check(credentials.values["openai_api_key"] == "original-dummy-key", "credentials restored")
        check(defaults.string(forKey: "projectDirectory") == "/keep/this/device/path", "local device path retained")
        check(tryString(repository.logURL) == "original-log", "selected log restored")
        let before = try BackupArchive.open(BackupArchive.read(recovery), password: password)
        check(try BackupPolicy.preferences(before.preferences)["openaiModel"] as? String == "modified-model", "encrypted recovery captures prior state")
        let mode = try FileManager.default.attributesOfItem(atPath: recovery.path)[.posixPermissions] as? NSNumber
        check(mode?.intValue == 0o600, "backup permission 0600")
        let dbMode = try FileManager.default.attributesOfItem(atPath: repository.databaseURL.path)[.posixPermissions] as? NSNumber
        check(dbMode?.intValue == 0o600, "restored database permission 0600")
        // Inject a partial Keychain write failure after DB/settings/log mutations.
        guard sqlite3_open(repository.databaseURL.path, &live) == SQLITE_OK else { fatalError("reopen test database") }
        sql("INSERT INTO conversations VALUES('two','rollback-me','[]','today',1);")
        sqlite3_close(live); live = nil
        defaults.set("before-failure", forKey: "openaiModel")
        credentials.values["openai_api_key"] = "before-failure-key"
        credentials.values.removeValue(forKey: "container_api_key_demo")
        try BackupArchive.writePrivate(Data("before-failure-log".utf8), to: repository.logURL)
        credentials.failNextWrite = true
        rejects("credential write failure") { _ = try repository.restore(decoded, selection: restore, password: password, appVersion: "test") }
        check(defaults.string(forKey: "openaiModel") == "before-failure", "settings rolled back")
        check(credentials.values["openai_api_key"] == "before-failure-key", "partial credential write rolled back")
        check(credentials.values["container_api_key_demo"] == nil, "new credential removed during rollback")
        check(tryString(repository.logURL) == "before-failure-log", "log rolled back")
        check(try BackupDatabase.validate(BackupDatabase.snapshot(repository.databaseURL)!).conversations == 2, "database rolled back")
        let settingsOnly = BackupRepository.Selection(database: false, settings: true, credentials: false, logs: false)
        _ = try repository.restore(decoded, selection: settingsOnly, password: password, appVersion: "test")
        check(credentials.values["openai_api_key"] == "before-failure-key", "unselected credentials preserved")
        check(tryString(repository.logURL) == "before-failure-log", "unselected logs preserved")
        try FileManager.default.removeItem(at: repository.registryURL)
        defaults.set(String(data: registry, encoding: .utf8), forKey: "containerRegistryJSON")
        credentials.failNextWrite = true
        rejects("rollback with registry draft only") {
            _ = try repository.restore(decoded, selection: restore, password: password, appVersion: "test")
        }
        check(!FileManager.default.fileExists(atPath: repository.registryURL.path), "absent registry file restored on rollback")
        check(defaults.string(forKey: "containerRegistryJSON") == String(data: registry, encoding: .utf8), "registry draft restored independently")
        let backup = directory.appendingPathComponent("export.aichatbackup")
        try BackupArchive.writePrivate(encrypted, to: backup)
        if CommandLine.arguments.count == 2 {
            try BackupArchive.writePrivate(encrypted, to: URL(fileURLWithPath: CommandLine.arguments[1]))
        }
        check(try FileManager.default.contentsOfDirectory(atPath: directory.path).allSatisfy { !$0.hasPrefix(".backup-") }, "staging files removed")
        print("PASS: \(checks) encrypted backup, WAL snapshot, validation, restore and rollback checks; isolated fixtures only.")
    }

    static func tryString(_ url: URL) -> String { (try? String(contentsOf: url, encoding: .utf8)) ?? "" }
}
