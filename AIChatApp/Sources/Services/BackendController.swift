import Darwin
import Foundation

/// 负责启动/停止本地 Python 后端（main.py），并把 API Key 通过环境变量传进去
@MainActor
final class BackendController: ObservableObject {
    enum Status: Equatable {
        case stopped
        case starting
        case running(external: Bool)
        case failed(String)

        var isRunning: Bool {
            if case .running = self { return true }
            return false
        }

        var text: String {
            switch self {
            case .stopped:
                return L("未运行")
            case .starting:
                return L("启动中…")
            case .running(let external):
                return external ? L("运行中（外部启动）") : L("运行中")
            case .failed(let message):
                return L("启动失败：{0}", message)
            }
        }
    }

    @Published private(set) var status: Status = .stopped
    /// 日志行（带稳定 id：避免列表用 offset 当 id 导致每次追加都整体重建）
    struct LogLine: Identifiable, Equatable {
        let id: Int
        let text: String
    }

    @Published private(set) var logs: [LogLine] = []
    /// 后端存储后端（sqlite / memory），来自 /api/health
    @Published private(set) var storageBackend: String?
    @Published private(set) var storageDetail: StorageDetail?
    /// 工具凭据状态，来自 /api/health 的 azure / meraki
    @Published private(set) var azureConfigured: Bool?
    @Published private(set) var merakiConfigured: Bool?
    /// Nexus Dashboard 工具凭据状态，来自 /api/health 的 nexus_dashboard
    @Published private(set) var ndConfigured: Bool?
    /// azure 的凭据来源：explicit / default_chain / missing
    @Published private(set) var azureCredentialSource: String?
    /// azure 探测失败原因（仅在 missing 时有值）
    @Published private(set) var azureProbeError: String?
    /// 当前跑的是 Bundle 里的内嵌后端，还是开发模式的 python main.py
    @Published private(set) var isUsingBundledBackend = false

    /// 后端是外部启动的（终端里跑的）：应用不知道它带的是哪份凭据，也管不了它的重启
    var isExternallyStarted: Bool {
        if case .running(external: true) = status { return true }
        return false
    }

    private var process: Process?
    private var stdoutPipe: Pipe?
    private var stderrPipe: Pipe?
    private let maxLogLines = 300
    private var logFileHandle: FileHandle?
    /// 日志先攒在缓冲区，最多每 250ms 发布一次，避免刷屏时 SwiftUI 每个 chunk 重绘一次
    private var pendingLogLines: [LogLine] = []
    private var pendingLogText = ""
    private var logFlushScheduled = false
    private var nextLogID = 0
    private var isStarting = false

    // MARK: - 路径

    /// Bundle 内嵌的后端。放在 Contents/MacOS 里，所以 forAuxiliaryExecutable 能找到
    static func bundledBackendURL(bundle: Bundle = .main) -> URL? {
        // 优先级：onedir（Contents/Resources/backend_server/backend_server）> onefile
        // 目录版没有每次启动的解包开销，冷启动从 ~20s 降到 ~1s
        var candidates: [URL?] = []
        if let resources = bundle.resourceURL {
            candidates.append(resources.appendingPathComponent("backend_server/backend_server"))
            candidates.append(resources.appendingPathComponent("backend/backend_server"))
        }
        candidates.append(
            bundle.bundleURL.appendingPathComponent("Contents/Resources/backend_server/backend_server")
        )
        // onefile：可执行文件直接放在 Contents/MacOS
        candidates.append(bundle.url(forAuxiliaryExecutable: "backend_server"))
        candidates.append(bundle.url(forResource: "backend_server", withExtension: nil))

        return candidates
            .compactMap { $0 }
            .first { FileManager.default.isExecutableFile(atPath: $0.path) }
    }

    /// 后端的工作目录 / SQLite 落点：~/Library/Application Support/AIChatApp
    private var supportDirectory: URL {
        let base = FileManager.default
            .urls(for: .applicationSupportDirectory, in: .userDomainMask)
            .first
            ?? FileManager.default.homeDirectoryForCurrentUser
                .appendingPathComponent("Library/Application Support", isDirectory: true)
        return base.appendingPathComponent("AIChatApp", isDirectory: true)
    }

    /// 后端日志文件：~/Library/Logs/AIChatApp/backend.log
    private var logFileURL: URL {
        let base = FileManager.default
            .urls(for: .libraryDirectory, in: .userDomainMask)
            .first
            ?? FileManager.default.homeDirectoryForCurrentUser
                .appendingPathComponent("Library", isDirectory: true)
        return base.appendingPathComponent("Logs/AIChatApp/backend.log")
    }

    /// 上次运行留下的 pid，用来清理崩溃后的残留进程
    private var pidFileURL: URL {
        supportDirectory.appendingPathComponent("backend.pid")
    }

    // MARK: - 运行架构（本项目只支持 Apple Silicon 原生 arm64）

    /// 原生 arm64 地址：用它强制挑 arm64 切片，避免 Rosetta 翻译
    static let archToolPath = "/usr/bin/arch"

    /// 当前 App 进程是否被 Rosetta 翻译运行（正常情况下应该永远是 false）
    static var isRunningTranslated: Bool {
        var value: Int32 = 0
        var size = MemoryLayout<Int32>.size
        let rc = sysctlbyname("sysctl.proc_translated", &value, &size, nil, 0)
        return rc == 0 && value == 1
    }

    /// App 自身架构描述（写进日志，方便一眼确认没走 Rosetta）
    static var architectureDescription: String {
        #if arch(arm64)
        let machine = "arm64"
        #else
        let machine = "x86_64"
        #endif
        if isRunningTranslated {
            return "\(machine)（⚠️ 正被 Rosetta 翻译运行，本项目要求原生 arm64）"
        }
        return "\(machine)（原生，未使用 Rosetta）"
    }

    /// 开发模式专用：确认解释器有 arm64 切片，并返回“用 arch -arm64 包一层”的可执行文件与参数
    static func nativePythonCommand(pythonPath: String, scriptArguments: [String]) -> (executable: String, arguments: [String]) {
        (archToolPath, ["-arm64", pythonPath] + scriptArguments)
    }

    // MARK: - 启动 / 停止

    func start(settings: AppSettings) {
        guard process == nil, !isStarting else { return }
        isStarting = true
        status = .starting

        // 先把日志文件打开，后面的「清理残留进程」也能落盘
        openLogFile()

        // 清理上次残留进程要跑 /bin/ps + 轮询等它退出，绝对不能放在主线程上（会卡住 UI）。
        // 这里丢到后台线程，干完再回主线程继续启动。
        let pidFile = pidFileURL
        Task { [weak self] in
            let report = await Task.detached(priority: .userInitiated) {
                Self.reapStaleBackendBlocking(pidFileURL: pidFile)
            }.value
            guard let self else { return }
            for line in report {
                self.record(line + "\n")
            }
            self.launch(settings: settings)
        }
    }

    /// 真正拉起进程（在主线程上执行，除进程启动外没有阻塞操作）
    private func launch(settings: AppSettings) {
        defer { isStarting = false }

        let fileManager = FileManager.default
        append("[app] App 运行架构：\(Self.architectureDescription)")

        var environment = ProcessInfo.processInfo.environment
        for (key, value) in settings.environmentOverrides() {
            environment[key] = value
        }
        environment["PYTHONUNBUFFERED"] = "1"

        let task = Process()
        let workingDirectory: URL

        if let bundled = Self.bundledBackendURL() {
            // 内嵌后端：工作目录放 Application Support，SQLite / 临时文件不写进 App Bundle
            try? fileManager.createDirectory(at: supportDirectory, withIntermediateDirectories: true)
            workingDirectory = supportDirectory
            task.executableURL = bundled
            task.arguments = []
            isUsingBundledBackend = true
            append("[app] 使用内嵌后端：\(bundled.path)")
            append("[app] 工作目录：\(workingDirectory.path)")
        } else {
            // 开发模式：项目目录 + .venv/bin/python main.py
            let directory = URL(fileURLWithPath: settings.projectDirectory, isDirectory: true)
            let scriptURL = directory.appendingPathComponent("main.py")

            guard fileManager.fileExists(atPath: scriptURL.path) else {
                status = .failed(L("Bundle 里没有内嵌后端，也没在 {0} 里找到 main.py", directory.path))
                return
            }
            guard fileManager.isExecutableFile(atPath: settings.pythonPath) else {
                status = .failed(L("Python 解释器不可执行：{0}", settings.pythonPath))
                return
            }

            if settings.writeDotEnvOnLaunch {
                do {
                    let url = try settings.writeDotEnv()
                    append("[app] 已写入 .env：\(url.path)")
                } catch {
                    append("[app] 写入 .env 失败：\(error.localizedDescription)")
                }
            }

            workingDirectory = directory
            // 开发模式也必须原生 arm64：用 /usr/bin/arch -arm64 挑 arm64 切片，
            // 解释器若没有 arm64 切片（Intel-only Python）会直接报 Bad CPU type 而不是走 Rosetta
            let command = Self.nativePythonCommand(
                pythonPath: settings.pythonPath,
                scriptArguments: ["main.py"]
            )
            task.executableURL = URL(fileURLWithPath: command.executable)
            task.arguments = command.arguments
            isUsingBundledBackend = false
            append("[app] 未找到内嵌后端，改用开发模式（强制原生 arm64）：\(settings.pythonPath) main.py")
        }

        task.currentDirectoryURL = workingDirectory
        task.environment = environment

        let outPipe = Pipe()
        let errPipe = Pipe()
        task.standardOutput = outPipe
        task.standardError = errPipe

        outPipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty, let text = String(data: data, encoding: .utf8) else { return }
            Task { @MainActor in self?.record(text) }
        }
        errPipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty, let text = String(data: data, encoding: .utf8) else { return }
            Task { @MainActor in self?.record(text) }
        }
        task.terminationHandler = { [weak self] finished in
            Task { @MainActor in
                self?.append("[app] 后端进程已退出，退出码 \(finished.terminationStatus)")
                self?.process = nil
                self?.stdoutPipe = nil
                self?.stderrPipe = nil
                self?.closeLogFile()
                self?.removePidFile()
                if let current = self?.status, current.isRunning {
                    self?.status = .stopped
                } else if case .starting? = self?.status {
                    self?.status = .failed(L("进程启动后立即退出，请看下方日志"))
                }
            }
        }

        do {
            try task.run()
        } catch {
            status = .failed(L("无法启动进程：{0}", error.localizedDescription))
            return
        }

        process = task
        stdoutPipe = outPipe
        stderrPipe = errPipe
        status = .starting
        writePidFile(task.processIdentifier)
        // 进程已经带着当前凭据起来了，记下指纹（测试连接靠它判断要不要重启）
        settings.markCredentialsApplied()
        append("[app] 已启动 \(task.executableURL?.lastPathComponent ?? "后端")（pid \(task.processIdentifier)）")
        append("[app] 日志文件：\(logFileURL.path)")

        let injectedKeys = settings.environmentOverrides().keys.sorted().joined(separator: ", ")
        append("[app] 注入环境变量：\(injectedKeys)")

        Task { await waitUntilHealthy(settings: settings) }
    }

    func stop() {
        guard let process else {
            if status.isRunning {
                status = .stopped
            }
            closeLogFile()
            removePidFile()
            return
        }

        stdoutPipe?.fileHandleForReading.readabilityHandler = nil
        stderrPipe?.fileHandleForReading.readabilityHandler = nil
        process.terminationHandler = nil
        process.terminate()

        self.process = nil
        self.stdoutPipe = nil
        self.stderrPipe = nil
        status = .stopped
        closeLogFile()
        removePidFile()
        append("[app] 已停止后端进程")
    }

    /// App 退出时调用：SIGTERM 之后最多等 5 秒，还活着就 SIGKILL
    func terminateOnAppExit() {
        defer {
            closeLogFile()
            removePidFile()
        }

        guard let process else { return }
        let pid = process.processIdentifier
        record("[app] App 退出：给后端 \(pid) 发 SIGTERM\n")

        if process.isRunning {
            process.terminate()
            // 退出路径上不能等太久，否则 App 关不掉（表现为「退出时卡死」）
            if !Self.waitUntilGoneBlocking(pid: pid, timeout: 2) {
                append("[app] 后端 2 秒内没退出，改用 SIGKILL")
                kill(pid, SIGKILL)
                // 万一它自成一个进程组，连组一起收掉
                kill(-pid, SIGKILL)
                _ = Self.waitUntilGoneBlocking(pid: pid, timeout: 0.5)
            }
        }

        self.process = nil
        self.stdoutPipe = nil
        self.stderrPipe = nil
        status = .stopped
    }

    func restart(settings: AppSettings) {
        stop()
        Task {
            try? await Task.sleep(nanoseconds: 500_000_000)
            start(settings: settings)
        }
    }

    func clearLogs() {
        logs.removeAll()
        pendingLogLines.removeAll()
        pendingLogText = ""
    }

    // MARK: - 日志文件 / 进程记录

    private func openLogFile() {
        let fileManager = FileManager.default
        try? fileManager.createDirectory(
            at: logFileURL.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        if !fileManager.fileExists(atPath: logFileURL.path) {
            fileManager.createFile(atPath: logFileURL.path, contents: nil)
        }

        logFileHandle = try? FileHandle(forWritingTo: logFileURL)
        logFileHandle?.seekToEndOfFile()
        record("\n===== \(Self.timestamp()) 启动后端 =====\n")
    }

    private func closeLogFile() {
        try? logFileHandle?.close()
        logFileHandle = nil
    }

    /// 既进 App 内日志面板，也落盘到 backend.log
    private func record(_ text: String) {
        append(text)
        pendingLogText += text
        scheduleLogFlush()
    }

    private static func timestamp() -> String {
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyy-MM-dd HH:mm:ss"
        return formatter.string(from: Date())
    }

    private func writePidFile(_ pid: Int32) {
        try? FileManager.default.createDirectory(
            at: supportDirectory,
            withIntermediateDirectories: true
        )
        try? String(pid).write(to: pidFileURL, atomically: true, encoding: .utf8)
    }

    private func removePidFile() {
        try? FileManager.default.removeItem(at: pidFileURL)
    }

    private func readPidFile() -> Int32? {
        guard let text = try? String(contentsOf: pidFileURL, encoding: .utf8)
            .trimmingCharacters(in: .whitespacesAndNewlines) else { return nil }
        return Int32(text)
    }

    /// App 崩溃（没走到 terminateOnAppExit）时后端会变成孤儿进程：
    /// 启动前先按 pid 文件把它清掉，避免端口被占 / 残留进程堆积。
    /// 纯阻塞实现：只在后台线程调用，返回要写进日志的文本
    nonisolated private static func reapStaleBackendBlocking(pidFileURL: URL) -> [String] {
        var report: [String] = []

        guard let pid = readPidFileBlocking(at: pidFileURL), pid > 0, pid != getpid() else {
            return report
        }

        guard isBackendProcessBlocking(pid: pid) else {
            // pid 被复用了，或者上次崩掉的后端已经自己退了
            report.append("[app] 上次记录的 pid \(pid) 已不是后端进程，清理记录")
            try? FileManager.default.removeItem(at: pidFileURL)
            return report
        }

        report.append("[app] 发现上次残留的后端进程（pid \(pid)），先结束它")
        kill(pid, SIGTERM)
        if !waitUntilGoneBlocking(pid: pid, timeout: 2) {
            kill(pid, SIGKILL)
            kill(-pid, SIGKILL)
            _ = waitUntilGoneBlocking(pid: pid, timeout: 1)
        }
        try? FileManager.default.removeItem(at: pidFileURL)
        return report
    }

    nonisolated private static func readPidFileBlocking(at url: URL) -> Int32? {
        guard let text = try? String(contentsOf: url, encoding: .utf8)
            .trimmingCharacters(in: .whitespacesAndNewlines) else { return nil }
        return Int32(text)
    }

    /// 确认这个 pid 真的是我们的后端（避免 pid 复用误杀别的进程）
    nonisolated private static func isBackendProcessBlocking(pid: Int32) -> Bool {
        guard kill(pid, 0) == 0 else { return false }

        let ps = Process()
        ps.executableURL = URL(fileURLWithPath: "/bin/ps")
        ps.arguments = ["-p", String(pid), "-o", "command="]
        let pipe = Pipe()
        ps.standardOutput = pipe
        ps.standardError = Pipe()

        do {
            try ps.run()
        } catch {
            return false
        }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        ps.waitUntilExit()

        let command = String(data: data, encoding: .utf8) ?? ""
        return command.contains("backend_server") || command.contains("main.py")
    }

    /// 等进程消失（SIGTERM 之后最多等 timeout 秒）
    nonisolated private static func waitUntilGoneBlocking(pid: Int32, timeout: TimeInterval) -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if kill(pid, 0) != 0 { return true }
            usleep(50_000)
        }
        return kill(pid, 0) != 0
    }

    /// 供外部展示：当前后端日志文件位置
    var logFilePath: String { logFileURL.path }

    // MARK: - 健康检查

    func probeHealth(settings: AppSettings) async -> HealthResponse? {
        let client = Self.cachedHealthClient(baseURLString: settings.backendBaseURL)
        let health = try? await client.health()
        if let health {
            storageBackend = health.storageType
            storageDetail = health.storageDetail
            azureConfigured = health.azure?.configured
            merakiConfigured = health.meraki?.configured
            ndConfigured = health.nexusDashboard?.configured
            azureCredentialSource = health.azure?.credentialSource
            azureProbeError = health.azure?.probeError
        }
        return health
    }

    /// 复用同一个 APIClient（URLSession）：健康检查在启动阶段最多轮询 40 次，
    /// 每次新建一个 session 会白白建一堆连接池。
    private static var healthClients: [String: APIClient] = [:]

    private static func cachedHealthClient(baseURLString: String) -> APIClient {
        if let cached = healthClients[baseURLString] {
            return cached
        }
        let client = APIClient(baseURLString: baseURLString, timeout: 5)
        healthClients[baseURLString] = client
        return client
    }

    /// Azure 凭据状态（检查清单用）
    enum ToolCredentialState: Equatable {
        case ok
        case probing
        case missing
    }

    var azureState: ToolCredentialState {
        switch azureCredentialSource {
        case "explicit", "default_chain":
            return .ok
        case "probing":
            return .probing
        case "missing":
            return .missing
        default:
            // 兼容没有 credential_source 的旧后端
            return azureConfigured == true ? .ok : .missing
        }
    }

    /// 检查清单文案：explicit / default_chain / probing / missing
    var azureStatusText: String {
        switch azureState {
        case .ok where azureCredentialSource == "explicit":
            return L("已配置（环境变量）")
        case .ok:
            return L("已配置（az login 凭据）")
        case .probing:
            return L("检测中…")
        case .missing:
            return L("未配置")
        }
    }

    var azureStatusOK: Bool {
        azureState == .ok
    }

    /// 后端可能不是本应用启动的（比如在终端里跑着），这里补一次探测
    func refreshExternalStatus(settings: AppSettings) async {
        guard process == nil else { return }
        if let health = await probeHealth(settings: settings), health.status == "ok" {
            status = .running(external: true)
        } else if status.isRunning {
            status = .stopped
        }
    }

    /// 内嵌后端是 PyInstaller 单文件，冷启动要把 ~30MB 解包到临时目录，
    /// 实测首次可连接要 20 秒以上（开发模式 3~5 秒）。等待时间必须比它长，
    /// 否则会在后端马上就好之前误报「启动失败」，用户看到的就是「无法连接本地后端」。
    static let startupTimeout: TimeInterval = {
        let configured = ProcessInfo.processInfo.environment["BACKEND_STARTUP_TIMEOUT_SECONDS"]
        return configured.flatMap(TimeInterval.init) ?? 90
    }()

    func waitUntilHealthy(settings: AppSettings, timeout: TimeInterval? = nil) async {
        let deadline = Date().addingTimeInterval(timeout ?? Self.startupTimeout)

        while Date() < deadline {
            if let health = await probeHealth(settings: settings), health.status == "ok" {
                status = .running(external: false)
                append("[app] 后端健康检查通过")
                return
            }
            if process == nil {
                status = .failed(L("进程已退出，请查看下方日志"))
                return
            }
            try? await Task.sleep(nanoseconds: 500_000_000)
        }
        status = .failed(
            L("后端 {0} 秒内没有响应，请检查依赖与日志", String(Int(timeout ?? Self.startupTimeout)))
        )
    }

    // MARK: - 依赖检查

    func checkDependencies(settings: AppSettings) async -> String {
        guard FileManager.default.isExecutableFile(atPath: settings.pythonPath) else {
            return L("Python 解释器不可执行：{0}", settings.pythonPath)
        }

        // 先确认解释器有 arm64 切片：本项目只跑原生 Apple Silicon，不用 Rosetta
        let archCommand = Self.nativePythonCommand(
            pythonPath: settings.pythonPath,
            scriptArguments: ["-c", "import platform; print(platform.machine())"]
        )
        let (archStatus, archOutput) = await runCapturingOutput(
            executable: archCommand.executable,
            arguments: archCommand.arguments,
            directory: URL(fileURLWithPath: settings.projectDirectory, isDirectory: true),
            environment: nil
        )
        let arch = archOutput.trimmingCharacters(in: .whitespacesAndNewlines)
        guard archStatus == 0, arch == "arm64" else {
            let detail = arch.isEmpty ? "退出码 \(archStatus)" : arch
            return """
            解释器不是原生 arm64：\(settings.pythonPath)
            详情：\(detail)

            本项目只支持 Apple Silicon 原生 arm64（不使用 Rosetta），请换成带 arm64 切片的 Python：
              · 原生 python.org 安装包 / 原生 Homebrew（/opt/homebrew）
              · Arm64 版 Conda 或 `uv python install` 装的 CPython
            然后重新创建 .venv：arch -arm64 <python> -m venv .venv
            """
        }

        let (status, output) = await runCapturingOutput(
            executable: archCommand.executable,
            arguments: ["-arm64", settings.pythonPath, "-c", "import fastapi, uvicorn, openai, httpx, dotenv; print('dependencies ok')"],
            directory: URL(fileURLWithPath: settings.projectDirectory, isDirectory: true),
            environment: nil
        )

        let trimmed = output.trimmingCharacters(in: .whitespacesAndNewlines)
        if status == 0 {
            return L("依赖检查通过（原生 arm64）：{0}", trimmed)
        }
        return """
        依赖缺失（退出码 \(status)）：
        \(trimmed)

        可在项目目录执行（保持原生 arm64）：
        arch -arm64 \(settings.pythonPath) -m pip install -r requirements-fastapi.txt
        """
    }

    private func runCapturingOutput(
        executable: String,
        arguments: [String],
        directory: URL?,
        environment: [String: String]?
    ) async -> (Int32, String) {
        await withCheckedContinuation { continuation in
            let task = Process()
            task.executableURL = URL(fileURLWithPath: executable)
            task.arguments = arguments
            if let directory {
                task.currentDirectoryURL = directory
            }
            if let environment {
                task.environment = environment
            }

            let pipe = Pipe()
            task.standardOutput = pipe
            task.standardError = pipe
            task.terminationHandler = { finished in
                let data = pipe.fileHandleForReading.readDataToEndOfFile()
                let text = String(data: data, encoding: .utf8) ?? ""
                continuation.resume(returning: (finished.terminationStatus, text))
            }

            do {
                try task.run()
            } catch {
                continuation.resume(
                    returning: (127, L("无法执行 {0}：{1}", executable, error.localizedDescription))
                )
            }
        }
    }

    // MARK: - 日志

    private func append(_ chunk: String) {
        let newLines = chunk
            .split(separator: "\n", omittingEmptySubsequences: false)
            .map { String($0).trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }

        guard !newLines.isEmpty else { return }
        for line in newLines {
            nextLogID += 1
            pendingLogLines.append(LogLine(id: nextLogID, text: line))
        }
        scheduleLogFlush()
    }

    /// 攒 250ms 再统一发布：刷屏时把「每行一次 SwiftUI 重绘」压成每秒最多 4 次
    private func scheduleLogFlush() {
        guard !logFlushScheduled else { return }
        logFlushScheduled = true

        Task { [weak self] in
            try? await Task.sleep(nanoseconds: 250_000_000)
            self?.flushLogs()
        }
    }

    private func flushLogs() {
        logFlushScheduled = false

        if !pendingLogLines.isEmpty {
            logs.append(contentsOf: pendingLogLines)
            pendingLogLines.removeAll(keepingCapacity: true)
            if logs.count > maxLogLines {
                logs.removeFirst(logs.count - maxLogLines)
            }
        }

        // 落盘放在后台线程，别让文件 IO 占着主线程
        if !pendingLogText.isEmpty {
            let text = pendingLogText
            pendingLogText = ""
            let handle = logFileHandle
            Task.detached(priority: .utility) {
                if let data = text.data(using: .utf8) {
                    try? handle?.write(contentsOf: data)
                }
            }
        }
    }
}
