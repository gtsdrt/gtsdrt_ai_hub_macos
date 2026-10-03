import CommonCrypto
import CryptoKit
import Darwin
import Foundation
import Security
import SQLite3

enum BackupError: LocalizedError {
    case password, invalidFile, authentication, tooLarge, database, credentials
    case localOnly, externalBackend, stopping, rollback(String)

    var errorDescription: String? {
        switch self {
        case .password: return L("备份密码至少需要 12 个字符，且不超过 1024 字节。")
        case .invalidFile: return L("备份格式不支持，或数据校验失败。")
        case .authentication: return L("密码不正确，或备份文件已损坏。")
        case .tooLarge: return L("备份超过 128 MB 限制，请减少日志或清理历史后重试。")
        case .database: return L("无法安全读取或恢复聊天数据库，请检查文件权限和数据库状态。")
        case .credentials: return L("无法读取或写入全部钥匙串凭据，操作已停止。")
        case .localOnly: return L("备份与恢复仅支持本机后端，请先切换到本机地址。")
        case .externalBackend: return L("请先停止外部启动的后端，再进行恢复。")
        case .stopping: return L("后端尚未完全停止，未执行恢复。")
        case .rollback(let path): return L("恢复及回滚未能全部完成。请保留并使用恢复前备份：{0}", path)
        }
    }
}

/// All private metadata is inside the authenticated ciphertext. No archive paths
/// are accepted: the payload has a fixed schema and the destination is local.
struct BackupPayload: Codable {
    let formatVersion: Int
    let createdAt: Date
    let appVersion: String
    let database: Data?
    let preferences: Data
    let registry: Data?
    let credentials: [String: String]?
    let log: Data?
}

struct BackupSummary {
    let conversations: Int
    let tasks: Int
}

enum BackupArchive {
    static let maximumSize = 128 * 1024 * 1024
    static let iterations: UInt32 = 600_000
    private static let magic = Data("AICHATBK1".utf8)
    private static let headerSize = 9 + 4 + 16

    static func seal(_ payload: BackupPayload, password: String) throws -> Data {
        guard password.count >= 12, password.utf8.count <= 1024 else { throw BackupError.password }
        let plaintext = try JSONEncoder().encode(payload)
        guard plaintext.count < maximumSize - headerSize - 28 else { throw BackupError.tooLarge }
        var salt = [UInt8](repeating: 0, count: 16)
        guard SecRandomCopyBytes(kSecRandomDefault, salt.count, &salt) == errSecSuccess else {
            throw BackupError.credentials
        }
        let header = magic + Data([
            UInt8(iterations >> 24), UInt8((iterations >> 16) & 255),
            UInt8((iterations >> 8) & 255), UInt8(iterations & 255)
        ]) + Data(salt)
        let key = try deriveKey(password: password, salt: Data(salt), rounds: iterations)
        let box = try AES.GCM.seal(plaintext, using: key, authenticating: header)
        guard let combined = box.combined else { throw BackupError.invalidFile }
        return header + combined
    }

    static func open(_ data: Data, password: String) throws -> BackupPayload {
        guard data.count <= maximumSize else { throw BackupError.tooLarge }
        guard data.count > headerSize + 28, data.prefix(magic.count) == magic,
              password.utf8.count <= 1024 else { throw BackupError.invalidFile }
        let rounds = data[9..<13].reduce(UInt32(0)) { ($0 << 8) | UInt32($1) }
        // Bound attacker-controlled work before key derivation; authenticate the
        // exact header (including KDF parameters) before decoding any payload.
        guard (iterations...2_000_000).contains(rounds) else { throw BackupError.invalidFile }
        let key = try deriveKey(password: password, salt: Data(data[13..<29]), rounds: rounds)
        let plaintext: Data
        do {
            let box = try AES.GCM.SealedBox(combined: data.dropFirst(headerSize))
            plaintext = try AES.GCM.open(box, using: key, authenticating: data.prefix(headerSize))
        } catch { throw BackupError.authentication }
        guard let payload = try? JSONDecoder().decode(BackupPayload.self, from: plaintext),
              payload.formatVersion == 1, payload.createdAt.timeIntervalSince1970.isFinite,
              payload.appVersion.count < 100 else { throw BackupError.invalidFile }
        return payload
    }

    private static func deriveKey(password: String, salt: Data, rounds: UInt32) throws -> SymmetricKey {
        var bytes = [UInt8](repeating: 0, count: 32)
        let passwordBytes = Data(password.utf8)
        let status = passwordBytes.withUnsafeBytes { passwordBuffer in
            salt.withUnsafeBytes { saltBuffer in
                CCKeyDerivationPBKDF(CCPBKDFAlgorithm(kCCPBKDF2),
                    passwordBuffer.baseAddress?.assumingMemoryBound(to: CChar.self), passwordBytes.count,
                    saltBuffer.baseAddress?.assumingMemoryBound(to: UInt8.self), salt.count,
                    CCPseudoRandomAlgorithm(kCCPRFHmacAlgSHA256), rounds, &bytes, bytes.count)
            }
        }
        guard status == kCCSuccess else { throw BackupError.invalidFile }
        let key = SymmetricKey(data: bytes)
        bytes.withUnsafeMutableBytes { buffer in _ = memset_s(buffer.baseAddress, buffer.count, 0, buffer.count) }
        return key
    }

    static func read(_ url: URL) throws -> Data {
        let size = try url.resourceValues(forKeys: [.fileSizeKey, .isRegularFileKey])
        guard size.isRegularFile == true else { throw BackupError.invalidFile }
        guard (size.fileSize ?? maximumSize + 1) <= maximumSize else { throw BackupError.tooLarge }
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }
        var data = Data()
        while let chunk = try handle.read(upToCount: 1024 * 1024), !chunk.isEmpty {
            guard data.count + chunk.count <= maximumSize else { throw BackupError.tooLarge }
            data.append(chunk)
        }
        return data
    }

    /// Create the staging file with 0600 before writing any bytes, then rename
    /// within the same directory. This works for databases and ciphertext alike.
    static func writePrivate(_ data: Data, to url: URL) throws {
        let fm = FileManager.default
        try fm.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true,
                               attributes: [.posixPermissions: 0o700])
        let stage = url.deletingLastPathComponent().appendingPathComponent(".backup-\(UUID().uuidString)")
        guard fm.createFile(atPath: stage.path, contents: nil, attributes: [.posixPermissions: 0o600]) else {
            throw BackupError.invalidFile
        }
        defer { try? fm.removeItem(at: stage) }
        let handle = try FileHandle(forWritingTo: stage)
        do {
            try handle.write(contentsOf: data)
            try handle.synchronize()
            try handle.close()
        } catch { try? handle.close(); throw error }
        guard rename(stage.path, url.path) == 0 else { throw BackupError.invalidFile }
    }
}

enum BackupDatabase {
    static func withTemporaryDirectory<T>(_ body: (URL) throws -> T) throws -> T {
        let fm = FileManager.default
        let url = fm.temporaryDirectory.appendingPathComponent("AIChatBackup-\(UUID().uuidString)")
        try fm.createDirectory(at: url, withIntermediateDirectories: false,
                               attributes: [.posixPermissions: 0o700])
        defer { try? fm.removeItem(at: url) }
        return try body(url)
    }

    static func snapshot(_ sourceURL: URL) throws -> Data? {
        guard FileManager.default.fileExists(atPath: sourceURL.path) else { return nil }
        return try withTemporaryDirectory { directory in
            let snapshotURL = directory.appendingPathComponent("snapshot.db")
            try BackupArchive.writePrivate(Data(), to: snapshotURL)
            var source: OpaquePointer?
            var destination: OpaquePointer?
            defer { sqlite3_close(source); sqlite3_close(destination) }
            guard sqlite3_open_v2(sourceURL.path, &source, SQLITE_OPEN_READONLY, nil) == SQLITE_OK,
                  sqlite3_open(snapshotURL.path, &destination) == SQLITE_OK,
                  let backup = sqlite3_backup_init(destination, "main", source, "main") else {
                throw BackupError.database
            }
            let deadline = Date().addingTimeInterval(10)
            var result: Int32
            repeat {
                result = sqlite3_backup_step(backup, 256)
                if result == SQLITE_BUSY || result == SQLITE_LOCKED { sqlite3_sleep(20) }
            } while (result == SQLITE_OK || result == SQLITE_BUSY || result == SQLITE_LOCKED) && Date() < deadline
            let finish = sqlite3_backup_finish(backup)
            guard result == SQLITE_DONE, finish == SQLITE_OK,
                  sqlite3_exec(destination, "PRAGMA journal_mode=DELETE", nil, nil, nil) == SQLITE_OK else {
                throw BackupError.database
            }
            // Close before reading the snapshot; no pending WAL pages may be omitted.
            sqlite3_close(destination)
            destination = nil
            let data = try BackupArchive.read(snapshotURL)
            _ = try validate(data)
            return data
        }
    }

    static func validate(_ data: Data) throws -> BackupSummary {
        guard data.count >= 100, data.prefix(16) == Data("SQLite format 3\0".utf8) else {
            throw BackupError.database
        }
        return try withTemporaryDirectory { directory in
            let url = directory.appendingPathComponent("validate.db")
            try BackupArchive.writePrivate(data, to: url)
            var db: OpaquePointer?
            defer { sqlite3_close(db) }
            guard sqlite3_open_v2(url.path, &db, SQLITE_OPEN_READONLY, nil) == SQLITE_OK else {
                throw BackupError.database
            }
            sqlite3_exec(db, "PRAGMA trusted_schema=OFF", nil, nil, nil)
            guard try strings(db, "PRAGMA quick_check") == ["ok"],
                  Set(try strings(db, "SELECT name FROM sqlite_master WHERE type='table'")) == ["conversations", "tasks"],
                  try strings(db, "SELECT name FROM sqlite_master WHERE type IN ('trigger','view')").isEmpty,
                  try strings(db, "SELECT name FROM sqlite_master WHERE type='table' AND upper(sql) LIKE 'CREATE VIRTUAL%'").isEmpty else {
                throw BackupError.database
            }
            // Fixed queries also reject incompatible schemas before any live data is touched.
            _ = try strings(db, "SELECT id,title,messages,updated_at,message_count FROM conversations LIMIT 0")
            _ = try strings(db, "SELECT instance_id,status,result,error,created_at,updated_at FROM tasks LIMIT 0")
            guard let chats = try strings(db, "SELECT COUNT(*) FROM conversations").first.flatMap(Int.init),
                  let tasks = try strings(db, "SELECT COUNT(*) FROM tasks").first.flatMap(Int.init) else {
                throw BackupError.database
            }
            return BackupSummary(conversations: chats, tasks: tasks)
        }
    }

    private static func strings(_ db: OpaquePointer?, _ sql: String) throws -> [String] {
        var statement: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &statement, nil) == SQLITE_OK else { throw BackupError.database }
        defer { sqlite3_finalize(statement) }
        var values: [String] = []
        var result = sqlite3_step(statement)
        while result == SQLITE_ROW {
            guard let text = sqlite3_column_text(statement, 0) else { throw BackupError.database }
            values.append(String(cString: text))
            result = sqlite3_step(statement)
        }
        guard result == SQLITE_DONE else { throw BackupError.database }
        return values
    }

    /// Caller must stop all writers before replacing or rolling back the database.
    static func install(_ data: Data?, at url: URL) throws {
        let fm = FileManager.default
        if let data { _ = try validate(data) }
        for suffix in ["-wal", "-shm"] {
            let path = url.path + suffix
            if fm.fileExists(atPath: path) { try fm.removeItem(atPath: path) }
        }
        if let data { try BackupArchive.writePrivate(data, to: url) }
        else if fm.fileExists(atPath: url.path) { try fm.removeItem(at: url) }
    }
}
