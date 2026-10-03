import AppKit
import Foundation

@MainActor
final class BackupController: ObservableObject {
    @Published private(set) var isBusy = false
    @Published private(set) var message: String?
    @Published private(set) var prepared: BackupPayload?
    @Published private(set) var summary: BackupSummary?
    @Published private(set) var recoveryURL: URL?
    private let session: SessionStore

    init(session: SessionStore) {
        self.session = session
        recoveryURL = session.backupRecoveryURL
    }

    func clearPreview(clearMessage: Bool = true) {
        prepared = nil; summary = nil
        if clearMessage { message = nil }
    }

    private var appVersion: String {
        Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "unknown"
    }

    private func repository() async throws -> BackupRepository {
        let settings = session.settings
        guard let host = URL(string: settings.backendBaseURL)?.host?.lowercased(),
              ["127.0.0.1", "localhost", "::1", "[::1]"].contains(host) else { throw BackupError.localOnly }
        let fm = FileManager.default
        let support = fm.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0].appendingPathComponent("AIChatApp")
        var path = ProcessInfo.processInfo.environment["AICHAT_DB_PATH"]
            ?? AppSettings.loadDotEnv(projectDirectory: settings.projectDirectory)["AICHAT_DB_PATH"]
            ?? support.appendingPathComponent("aichat.db").path
        if session.backend.status.isRunning || session.backend.status == .starting {
            session.syncAPIClient()
            let health = try await session.api.health()
            guard health.storageDetail?.resolvedType == "sqlite",
                  let actualPath = health.storageDetail?.dbPath else { throw BackupError.database }
            path = actualPath
        }
        path = (path as NSString).expandingTildeInPath
        let usesBundle = session.backend.isUsingBundledBackend || BackendController.bundledBackendURL() != nil
        let workingDirectory = usesBundle ? support : URL(fileURLWithPath: settings.projectDirectory, isDirectory: true)
        let database = URL(fileURLWithPath: path, relativeTo: workingDirectory).standardizedFileURL
        // AppSettings explicitly injects this path, overriding inherited/.env
        // values when launching the backend. Use the same authoritative path.
        let registry = URL(fileURLWithPath: AppSettings.containerRegistryPath()).standardizedFileURL
        return BackupRepository(databaseURL: database, registryURL: registry,
            logURL: URL(fileURLWithPath: session.backend.logFilePath),
            recoveryDirectory: support.appendingPathComponent("Backups"),
            defaults: .standard, credentialStore: KeychainStore.shared)
    }

    func create(to url: URL, password: String, includeCredentials: Bool, includeLogs: Bool) async -> Bool {
        guard !isBusy else { return false }
        isBusy = true; message = nil
        defer { isBusy = false }
        do {
            let repository = try await repository()
            guard ![repository.databaseURL, repository.registryURL, repository.logURL].contains(url.standardizedFileURL) else {
                throw BackupError.invalidFile
            }
            let version = appVersion
            try await Task.detached(priority: .userInitiated) {
                let payload = try repository.capture(includeCredentials: includeCredentials,
                    includeLogs: includeLogs, appVersion: version)
                try BackupArchive.writePrivate(BackupArchive.seal(payload, password: password), to: url)
            }.value
            message = L("加密备份已保存：{0}", url.path)
            return true
        } catch { message = error.localizedDescription; return false }
    }

    func inspect(_ url: URL, password: String) async {
        guard !isBusy else { return }
        isBusy = true; clearPreview()
        defer { isBusy = false }
        do {
            let (payload, counts) = try await Task.detached(priority: .userInitiated) {
                let payload = try BackupArchive.open(BackupArchive.read(url), password: password)
                return (payload, try BackupPolicy.validate(payload))
            }.value
            prepared = payload; summary = counts
        } catch { message = error.localizedDescription }
    }

    func restore(password: String, selection: BackupRepository.Selection) async -> Bool {
        guard !isBusy, let payload = prepared,
              selection.database || selection.settings || selection.credentials || selection.logs else { return false }
        isBusy = true; session.isRestoringBackup = true; message = nil
        let wasRunning = session.backend.status.isRunning
        var stopped = false
        var restartAllowed = true
        defer {
            session.isRestoringBackup = false
            isBusy = false
            if stopped && restartAllowed && (wasRunning || session.settings.autoStartBackend) {
                session.backend.start(settings: session.settings)
            }
        }
        do {
            guard !session.backend.isExternallyStarted else { throw BackupError.externalBackend }
            let repository = try await repository()
            try await session.backend.stopForDataRestore()
            stopped = true
            // A fresh local health response here means another writer still owns
            // the port. Refuse to swap a database beneath an external process.
            if (try? await session.api.health()) != nil {
                restartAllowed = false
                await session.backend.refreshExternalStatus(settings: session.settings)
                throw BackupError.externalBackend
            }
            let version = appVersion
            recoveryURL = try await Task.detached(priority: .userInitiated) {
                try repository.restore(payload, selection: selection, password: password, appVersion: version)
            }.value
            session.backupRecoveryURL = recoveryURL
            session.didRestoreBackup()
            prepared = nil; summary = nil
            message = L("备份已恢复，请重新登录。")
            return true
        } catch {
            if let failure = error as? BackupError, case .rollback(let path) = failure {
                restartAllowed = false
                recoveryURL = URL(fileURLWithPath: path)
                session.backupRecoveryURL = recoveryURL
            }
            message = error.localizedDescription
            return false
        }
    }
}
