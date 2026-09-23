import AppKit
import SwiftUI

struct SettingsView: View {
    @ObservedObject var session: SessionStore
    @ObservedObject var settings: AppSettings
    @ObservedObject var backend: BackendController

    /// 打开时滚动到哪个区域（从登录页的检查清单跳过来）
    let scrollTarget: SettingsSection?
    /// 作为 sheet 打开时的关闭回调；为 nil 表示嵌在设置 tab 里
    let onClose: (() -> Void)?

    @State private var statusMessage: String?
    @State private var dependencyReport: String?
    @State private var isCheckingDependencies = false
    @StateObject private var tester: ConnectionTester
    @State private var showingAzureInfo = false
    /// 项目目录 / Python 解释器属于开发模式选项，默认藏起来
    @State private var showingAdvancedOptions = false
    @Environment(\.appFontScale) private var fontScale
    @Environment(\.loc) private var loc

    init(session: SessionStore) {
        self.init(session: session, scrollTarget: nil, onClose: nil)
    }

    init(session: SessionStore, scrollTarget: SettingsSection?, onClose: (() -> Void)?) {
        self.session = session
        self.settings = session.settings
        self.backend = session.backend
        self.scrollTarget = scrollTarget
        self.onClose = onClose
        _tester = StateObject(wrappedValue: ConnectionTester(session: session))
    }

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    if let onClose {
                        HStack {
                            Text(loc.t("设置"))
                                .appFont(.title2)
                                .bold()
                            Spacer()
                            Button(loc.t("关闭")) { onClose() }
                                .keyboardShortcut(.cancelAction)
                        }
                    }

                    languageSection.id(SettingsSection.language)
                    backendSection.id(SettingsSection.backend)
                    azureSection.id(SettingsSection.azure)
                    merakiSection.id(SettingsSection.meraki)
                    nexusDashboardSection.id(SettingsSection.nexusDashboard)
                    providerSection.id(SettingsSection.provider)
                    loginSection.id(SettingsSection.login)
                    logSection.id(SettingsSection.log)
                }
                .padding(20)
            }
            .task {
                guard let scrollTarget else { return }
                // 等布局完成再滚动，否则目标区域还没有位置
                try? await Task.sleep(nanoseconds: 200_000_000)
                withAnimation {
                    proxy.scrollTo(scrollTarget, anchor: .top)
                }
            }
            // 改动任一凭据字段 / 切换 provider 时清空测试结果
            .onChange(of: credentialsSignature) { _ in
                tester.clearAll()
            }
        }
    }

    /// 页面上所有凭据字段的签名，用来感知「用户改了东西」
    private var credentialsSignature: String {
        [
            settings.azureTenantID,
            settings.azureClientID,
            settings.azureClientSecret,
            settings.azureSubscriptionID,
            settings.merakiAPIKey,
            settings.ndBaseURL,
            settings.ndUsername,
            settings.ndAPIKey,
            settings.ndPassword,
            settings.ndLoginDomain,
            settings.ndVerifyTLS ? "tls-on" : "tls-off",
            settings.deepseekKey,
            settings.kimiKey,
            settings.defaultProvider,
            settings.googleClientID,
            settings.googleAllowedEmails,
            settings.googleAllowedDomains,
            settings.githubClientID,
            settings.githubAllowedLogins,
            settings.githubAllowedEmails,
        ].joined(separator: "\u{1}")
    }

    // MARK: - 界面语言

    /// 界面语言：中文 / English / Norsk（Bokmål）/ 跟随系统
    private var languageSection: some View {
        SettingsSectionBox(title: loc.t("界面语言")) {
            VStack(alignment: .leading, spacing: 10) {
                SettingRow(title: loc.t("语言")) {
                    Picker("", selection: $settings.language) {
                        ForEach(AppLanguage.pickerOrder) { language in
                            // 三种语言写自己的名字；「跟随系统」跟着当前界面语言走
                            Text(loc.t(language.displayName)).tag(language)
                        }
                    }
                    .labelsHidden()
                    .pickerStyle(.segmented)
                    .frame(width: CGFloat(360).appScaled(by: fontScale))
                }

                Text(loc.t("切换语言后立即生效，不用重启应用。"))
                    .appFont(.caption)
                    .foregroundStyle(.secondary)
            }
            .padding(6)
        }
    }

    // MARK: - 本地后端

    private var backendSection: some View {
        SettingsSectionBox(title: loc.t("本地 Python 后端")) {
            VStack(alignment: .leading, spacing: 10) {
                SettingRow(title: loc.t("后端地址")) {
                    TextField("http://127.0.0.1:8000", text: $settings.backendBaseURL)
                        .textFieldStyle(.roundedBorder)
                }

                Toggle(loc.t("启动应用时自动拉起本地后端"), isOn: $settings.autoStartBackend)
                Toggle(loc.t("启动时把设置写入项目根目录的 .env"), isOn: $settings.writeDotEnvOnLaunch)

                Text(backendModeText)
                    .appFont(.caption)
                    .foregroundStyle(.secondary)

                advancedOptions

                Divider()

                HStack(spacing: 6) {
                    Text(loc.t("存储后端："))
                        .foregroundStyle(.secondary)
                    Text(backend.storageBackend ?? loc.t("未知（后端未运行）"))
                        .bold()
                    if let dbPath = backend.storageDetail?.dbPath {
                        Text(dbPath)
                            .appFont(.caption)
                            .foregroundStyle(.secondary)
                            .textSelection(.enabled)
                    }
                    if let error = backend.storageDetail?.error {
                        Text(loc.t("（已回退内存：{0}）", error))
                            .appFont(.caption)
                            .foregroundStyle(.orange)
                    }
                }

                HStack(spacing: 12) {
                    Text(loc.t("工具凭据："))
                        .foregroundStyle(.secondary)
                    HStack(spacing: 4) {
                        Image(systemName: azureSymbol)
                            .foregroundStyle(azureColor)
                        Text(loc.t("Azure：{0}", backend.azureStatusText))
                        if backend.azureState == .missing,
                           let probeError = backend.azureProbeError,
                           !probeError.isEmpty {
                            Text(probeError)
                                .appFont(.caption)
                                .foregroundStyle(.red)
                                .lineLimit(1)
                                .truncationMode(.middle)
                                .help(probeError)
                                .textSelection(.enabled)
                        }
                    }
                    Text(
                        loc.t(
                            "Meraki：{0}",
                            backend.merakiConfigured == true ? loc.t("已配置") : loc.t("未配置")
                        )
                    )
                    Text(loc.t("（Azure 支持环境变量或 az login 凭据；enable_tools=true 时才会调用）"))
                        .appFont(.caption)
                        .foregroundStyle(.secondary)
                }

                HStack(spacing: 10) {
                    StatusBadge(text: backend.status.text, isActive: backend.status.isRunning)
                    Spacer()
                    Button(loc.t("启动")) { backend.start(settings: settings) }
                        .disabled(backend.status.isRunning || backend.status == .starting)
                    Button(loc.t("停止")) { backend.stop() }
                        .disabled(!backend.status.isRunning)
                    Button(loc.t("重启")) {
                        settings.persistQuietly()
                        backend.restart(settings: settings)
                    }
                }
            }
            .padding(6)
        }
    }

    /// 当前后端模式（内嵌 / 开发）+ 日志位置
    private var backendModeText: String {
        if backend.isUsingBundledBackend {
            return loc.t("后端模式：内嵌 backend_server｜日志：{0}", backend.logFilePath)
        }
        if BackendController.bundledBackendURL() != nil {
            return loc.t("后端模式：内嵌 backend_server（下次启动生效）｜日志：{0}", backend.logFilePath)
        }
        return loc.t("后端模式：开发模式（没找到内嵌 backend_server，回退到项目目录 + Python）")
    }

    /// 开发模式才需要的选项，默认折叠
    private var advancedOptions: some View {
        DisclosureGroup(isExpanded: $showingAdvancedOptions) {
            VStack(alignment: .leading, spacing: 10) {
                SettingRow(title: loc.t("项目目录")) {
                    HStack(spacing: 8) {
                        TextField("/Users/you/Documents/MyMacApp", text: $settings.projectDirectory)
                            .textFieldStyle(.roundedBorder)
                        Button(loc.t("选择…")) { chooseProjectDirectory() }
                    }
                }

                SettingRow(title: loc.t("Python 解释器")) {
                    HStack(spacing: 8) {
                        TextField("/path/to/.venv/bin/python", text: $settings.pythonPath)
                            .textFieldStyle(.roundedBorder)
                        Button(loc.t("自动探测")) {
                            settings.pythonPath = AppSettings.detectPythonPath(projectDirectory: settings.projectDirectory)
                        }
                    }
                }

                Text(loc.t("只有在没有内嵌后端、需要跑项目里的 main.py 时才会用到这两项。"))
                    .appFont(.caption)
                    .foregroundStyle(.secondary)

                HStack(spacing: 10) {
                    Button(loc.t("检查依赖")) { checkDependencies() }
                        .disabled(isCheckingDependencies)
                    Button(loc.t("打开后端日志")) {
                        NSWorkspace.shared.selectFile(backend.logFilePath, inFileViewerRootedAtPath: "")
                    }
                }

                if let report = dependencyReport {
                    Text(report)
                        .appFont(.caption, design: .monospaced)
                        .textSelection(.enabled)
                        .padding(8)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(RoundedRectangle(cornerRadius: 6).fill(Color.secondary.opacity(0.08)))
                }
            }
            .padding(.top, 8)
            .padding(.leading, 4)
        } label: {
            Text(loc.t("高级选项（开发模式）"))
                .appFont(.caption)
                .foregroundStyle(.secondary)
        }
    }

    // MARK: - Azure 工具凭据

    private var azureSection: some View {
        SettingsSectionBox(title: loc.t("Azure 工具（enable_tools=true 时使用）")) {
            VStack(alignment: .leading, spacing: 10) {
                SettingRow(title: "Tenant ID") {
                    HStack(spacing: 6) {
                        TextField(loc.t("租户 ID"), text: $settings.azureTenantID)
                            .textFieldStyle(.roundedBorder)
                        FieldHelpIcon(help: loc.t("Azure Portal → Microsoft Entra ID → 概述 → 租户 ID"))
                    }
                }

                SettingRow(title: "Client ID") {
                    HStack(spacing: 6) {
                        TextField(loc.t("应用（客户端）ID"), text: $settings.azureClientID)
                            .textFieldStyle(.roundedBorder)
                        FieldHelpIcon(help: loc.t("Azure Portal → 应用注册 → 你的应用 → 应用程序(客户端) ID"))
                    }
                }

                SettingRow(title: "Client Secret") {
                    HStack(spacing: 6) {
                        SecureField(loc.t("客户端密码的 Value"), text: $settings.azureClientSecret)
                            .textFieldStyle(.roundedBorder)
                        FieldHelpIcon(help: loc.t("应用注册 → 证书和密码 → 新客户端密码（复制 Value，不是 Secret ID）"))
                    }
                }

                SettingRow(title: "Subscription ID") {
                    HStack(spacing: 6) {
                        TextField(loc.t("默认订阅 ID（可留空）"), text: $settings.azureSubscriptionID)
                            .textFieldStyle(.roundedBorder)
                        FieldHelpIcon(help: loc.t("Azure Portal → 订阅 → 订阅 ID"))
                    }
                }

                HStack(spacing: 10) {
                    Button(loc.t("保存 Azure 凭据")) { saveSettings() }
                    ConnectionTestButton(
                        title: loc.t("测试连接"),
                        isRunning: tester.isRunning(ConnectionTester.Target.azure)
                    ) {
                        tester.test(target: ConnectionTester.Target.azure)
                    }
                    Button {
                        showingAzureInfo = true
                    } label: {
                        Image(systemName: "info.circle")
                    }
                    .buttonStyle(.borderless)
                    .help(loc.t("测试连接会做什么"))
                    .popover(isPresented: $showingAzureInfo, arrowEdge: .bottom) {
                        azureInfoPopover
                    }
                }

                Text(
                    loc.t(
                        "当前后端进程：{0}。凭据改动需要重启后端才会生效。",
                        backend.azureConfigured == true
                            ? loc.t("已检测到 Azure 凭据")
                            : loc.t("未检测到 Azure 凭据")
                    )
                )
                    .appFont(.caption)
                    .foregroundStyle(.secondary)

                ConnectionTestResultView(state: tester.state(for: ConnectionTester.Target.azure)) {
                    tester.restartBackendAndTest(target: ConnectionTester.Target.azure)
                }
            }
            .padding(6)
        }
    }

    private var azureInfoPopover: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(loc.t("测试连接会做什么"))
                .appFont(.headline)
            Text(loc.t("直接调用 Azure 工具列出订阅：不走 AI、不消耗 token，默认 15 秒超时。"))
            Text(loc.t("测试前会先保存当前页面输入，并在需要时重启本地后端，保证测的是这份凭据而不是上次的。"))
            Text(loc.t("每个字段的取值路径：把鼠标停在字段右边的 ? 上。"))
        }
        .appFont(.caption)
        .frame(width: CGFloat(300).appScaled(by: fontScale), alignment: .leading)
        .padding(12)
    }

    // MARK: - Meraki 工具凭据

    private var merakiSection: some View {
        SettingsSectionBox(title: loc.t("Meraki 工具（enable_tools=true 时使用）")) {
            VStack(alignment: .leading, spacing: 10) {
                SettingRow(title: "Meraki API Key") {
                    HStack(spacing: 6) {
                        SecureField("Dashboard API Key", text: $settings.merakiAPIKey)
                            .textFieldStyle(.roundedBorder)
                        FieldHelpIcon(help: "Meraki Dashboard → Organization → Settings → Dashboard API access → Generate API key")
                    }
                }

                HStack(spacing: 10) {
                    Button(loc.t("保存 Meraki 凭据")) { saveSettings() }
                    ConnectionTestButton(
                        title: loc.t("测试连接"),
                        isRunning: tester.isRunning(ConnectionTester.Target.meraki)
                    ) {
                        tester.test(target: ConnectionTester.Target.meraki)
                    }
                }

                Text(
                    loc.t(
                        "当前后端进程：{0}。凭据改动需要重启后端才会生效。",
                        backend.merakiConfigured == true
                            ? loc.t("已检测到 Meraki 凭据")
                            : loc.t("未检测到 Meraki 凭据")
                    )
                )
                    .appFont(.caption)
                    .foregroundStyle(.secondary)

                ConnectionTestResultView(state: tester.state(for: ConnectionTester.Target.meraki)) {
                    tester.restartBackendAndTest(target: ConnectionTester.Target.meraki)
                }
            }
            .padding(6)
        }
    }

    // MARK: - Nexus Dashboard 工具凭据

    private var nexusDashboardSection: some View {
        SettingsSectionBox(title: loc.t("Nexus Dashboard 工具（官方 Infra API + Manage API）")) {
            VStack(alignment: .leading, spacing: 10) {
                SettingRow(title: "ND_BASE_URL") {
                    HStack(spacing: 6) {
                        TextField("https://nd.example.com", text: $settings.ndBaseURL)
                            .textFieldStyle(.roundedBorder)
                        FieldHelpIcon(help: loc.t("Nexus Dashboard 集群地址：只填主机名/IP，不要带 /api/v1/...（后端自动拼 Infra 与 Manage 两套基地址）"))
                    }
                }

                SettingRow(title: "ND_USERNAME") {
                    HStack(spacing: 6) {
                        TextField("admin", text: $settings.ndUsername)
                            .textFieldStyle(.roundedBorder)
                        FieldHelpIcon(help: loc.t("Nexus Dashboard 的本地账号（API Key 只对 local 账号有效）"))
                    }
                }

                SettingRow(title: "ND_API_KEY") {
                    HStack(spacing: 6) {
                        SecureField(loc.t("留空则改用下面的用户名密码登录"), text: $settings.ndAPIKey)
                            .textFieldStyle(.roundedBorder)
                        FieldHelpIcon(help: loc.t("右上角用户名 → Manage API keys → Add API key。留空则后端用 ND_PASSWORD 调 /api/v1/infra/login 换 token"))
                    }
                }

                SettingRow(title: "ND_PASSWORD") {
                    HStack(spacing: 6) {
                        SecureField(loc.t("仅在不用 API Key 时填写"), text: $settings.ndPassword)
                            .textFieldStyle(.roundedBorder)
                        FieldHelpIcon(help: loc.t("密码模式：后端 POST /api/v1/infra/login 拿 jwttoken，进程内缓存 10 分钟后自动续期"))
                    }
                }

                SettingRow(title: "ND_LOGIN_DOMAIN") {
                    HStack(spacing: 6) {
                        TextField("local", text: $settings.ndLoginDomain)
                            .textFieldStyle(.roundedBorder)
                        FieldHelpIcon(help: loc.t("密码模式的登录域，本地账号填 local；对接外部认证域时填对应域名"))
                    }
                }

                Toggle(loc.t("校验 TLS 证书（自签证书请保持关闭）"), isOn: $settings.ndVerifyTLS)

                HStack(spacing: 10) {
                    Button(loc.t("保存 Nexus Dashboard 凭据")) { saveSettings() }
                    ConnectionTestButton(
                        title: loc.t("测试连接"),
                        isRunning: tester.isRunning(ConnectionTester.Target.nexusDashboard)
                    ) {
                        tester.test(target: ConnectionTester.Target.nexusDashboard)
                    }
                }

                Text(
                    loc.t(
                        "当前后端进程：{0}。凭据改动需要重启后端才会生效。",
                        backend.ndConfigured == true
                            ? loc.t("已检测到 Nexus Dashboard 凭据")
                            : loc.t("未检测到 Nexus Dashboard 凭据")
                    )
                )
                    .appFont(.caption)
                    .foregroundStyle(.secondary)

                Text(loc.t("只读工具：Infra API 覆盖集群/节点/健康/容量/许可/租户/集成，Manage API 覆盖 fabric/交换机/接口/网络/VRF，不含任何写操作。"))
                    .appFont(.caption)
                    .foregroundStyle(.secondary)

                ConnectionTestResultView(state: tester.state(for: ConnectionTester.Target.nexusDashboard)) {
                    tester.restartBackendAndTest(target: ConnectionTester.Target.nexusDashboard)
                }
            }
            .padding(6)
        }
    }

    // MARK: - AI Provider

    private var providerSection: some View {
        SettingsSectionBox(title: loc.t("AI Provider（密钥存 Keychain，启动时传给 Python）")) {
            VStack(alignment: .leading, spacing: 10) {
                SettingRow(title: loc.t("默认 Provider")) {
                    Picker("", selection: $settings.defaultProvider) {
                        ForEach(AppSettings.providerOptions, id: \.self) { provider in
                            Text(provider).tag(provider)
                        }
                    }
                    .labelsHidden()
                    .pickerStyle(.segmented)
                    .frame(width: CGFloat(220).appScaled(by: fontScale))
                }

                SettingRow(title: "DEEPSEEK_API_KEY") {
                    HStack(spacing: 6) {
                        SecureField(loc.t("sk-…（留空则 DeepSeek 走 mock 回复）"), text: $settings.deepseekKey)
                            .textFieldStyle(.roundedBorder)
                        FieldHelpIcon(help: "platform.deepseek.com → API Keys")
                    }
                }

                SettingRow(title: "KIMI_API_KEY") {
                    HStack(spacing: 6) {
                        SecureField(loc.t("sk-…（留空则 Kimi 走 mock 回复）"), text: $settings.kimiKey)
                            .textFieldStyle(.roundedBorder)
                        FieldHelpIcon(help: loc.t("Moonshot / Kimi 开放平台 → API Keys"))
                    }
                }

                SettingRow(title: "OPENAI_API_KEY") {
                    HStack(spacing: 6) {
                        SecureField(loc.t("sk-…（留空则 OpenAI 走 mock 回复）"), text: $settings.openaiKey)
                            .textFieldStyle(.roundedBorder)
                        FieldHelpIcon(help: loc.t("OpenAI: platform.openai.com → API keys；Azure Foundry 用 Azure 门户里的 Key"))
                    }
                }

                SettingRow(title: "OPENAI_BASE_URL") {
                    HStack(spacing: 6) {
                        TextField("https://api.openai.com/v1", text: $settings.openaiBaseURL)
                            .textFieldStyle(.roundedBorder)
                        FieldHelpIcon(help: loc.t("Azure Foundry 填 https://<资源名>.openai.azure.com/openai/v1"))
                    }
                }

                SettingRow(title: "OPENAI_MODEL_NAME") {
                    HStack(spacing: 6) {
                        TextField("gpt-4o", text: $settings.openaiModel)
                            .textFieldStyle(.roundedBorder)
                        FieldHelpIcon(help: loc.t("Azure 上填部署名（例如 gpt-5.6-sol）；OpenAI 上填模型名"))
                    }
                }

                SettingRow(title: "OPENAI_AUTH_MODE") {
                    Picker("", selection: $settings.openaiAuthMode) {
                        Text("auto").tag("auto")
                        Text("bearer").tag("bearer")
                        Text("api-key").tag("api-key")
                    }
                    .labelsHidden()
                    .pickerStyle(.segmented)
                    .frame(width: CGFloat(220).appScaled(by: fontScale))
                }

                HStack(spacing: 10) {
                    Button(loc.t("保存设置")) { saveSettings() }
                    Button(loc.t("写入 .env")) { writeDotEnv() }
                    Button(loc.t("保存并重启后端")) {
                        saveSettings()
                        backend.restart(settings: settings)
                    }
                    ConnectionTestButton(
                        title: loc.t(
                            "测试余额（{0}）",
                            AppSettings.displayName(for: settings.defaultProvider)
                        ),
                        isRunning: tester.isRunning(settings.defaultProvider),
                        systemImage: "creditcard"
                    ) {
                        tester.test(target: settings.defaultProvider)
                    }
                }

                ConnectionTestResultView(state: tester.state(for: settings.defaultProvider)) {
                    tester.restartBackendAndTest(target: settings.defaultProvider)
                }

                if let statusMessage {
                    Text(statusMessage)
                        .appFont(.caption)
                        .foregroundStyle(.secondary)
                        .textSelection(.enabled)
                }
            }
            .padding(6)
        }
    }

    // MARK: - 登录

    private var loginSection: some View {
        SettingsSectionBox(title: loc.t("登录")) {
            VStack(alignment: .leading, spacing: 12) {
                Text("Google OAuth")
                    .appFont(.headline)

                SettingRow(title: "Google Client ID") {
                    TextField("xxxx.apps.googleusercontent.com", text: $settings.googleClientID)
                        .textFieldStyle(.roundedBorder)
                }

                SettingRow(title: loc.t("允许的邮箱")) {
                    TextField("alice@example.com,bob@example.com", text: $settings.googleAllowedEmails)
                        .textFieldStyle(.roundedBorder)
                }

                SettingRow(title: loc.t("允许的域名")) {
                    TextField("example.com", text: $settings.googleAllowedDomains)
                        .textFieldStyle(.roundedBorder)
                }

                Text(loc.t("Client ID 来自 Google Cloud 的桌面应用 OAuth Client。邮箱和域名用逗号分隔；两者都留空会允许所有 Google 账号。"))
                    .appFont(.caption)
                    .foregroundStyle(.secondary)

                Button(loc.t("保存并重启后端")) {
                    saveSettings()
                    backend.restart(settings: settings)
                }

                Divider()

                Text("GitHub OAuth（Device Flow）")
                    .appFont(.headline)

                SettingRow(title: "GitHub Client ID") {
                    TextField("OAuth App Client ID", text: $settings.githubClientID)
                        .textFieldStyle(.roundedBorder)
                }

                SettingRow(title: loc.t("允许的用户名")) {
                    TextField("octocat,another-user", text: $settings.githubAllowedLogins)
                        .textFieldStyle(.roundedBorder)
                }

                SettingRow(title: loc.t("允许的邮箱")) {
                    TextField("alice@example.com", text: $settings.githubAllowedEmails)
                        .textFieldStyle(.roundedBorder)
                }

                Text(loc.t("需要在 GitHub OAuth App 设置中启用 Device Flow。用户名和邮箱用逗号分隔；两者都留空会允许所有 GitHub 账号。"))
                    .appFont(.caption)
                    .foregroundStyle(.secondary)

                Button(loc.t("保存并重启后端")) {
                    saveSettings()
                    backend.restart(settings: settings)
                }

                Divider()

                Text(loc.t("本地管理员（应急入口）"))
                    .appFont(.headline)

                SettingRow(title: loc.t("用户名")) {
                    TextField("admin", text: $settings.username)
                        .textFieldStyle(.roundedBorder)
                }

                SettingRow(title: loc.t("密码")) {
                    SecureField("password123", text: $settings.loginPassword)
                        .textFieldStyle(.roundedBorder)
                }

                Text(loc.t("默认 admin / password123，可在后端 .env 里用 ADMIN_USERNAME、ADMIN_PASSWORD 覆盖。"))
                    .appFont(.caption)
                    .foregroundStyle(.secondary)

                HStack(spacing: 10) {
                    Button(loc.t("保存到 Keychain")) { saveSettings() }
                    Button(loc.t("退出登录")) {
                        session.logout(message: loc.t("已退出登录"))
                    }
                }
            }
            .padding(6)
        }
    }

    // MARK: - 日志

    private var logSection: some View {
        SettingsSectionBox(title: loc.t("后端日志")) {
            VStack(alignment: .leading, spacing: 8) {
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 2) {
                        if backend.logs.isEmpty {
                            Text(loc.t("暂无日志")).foregroundStyle(.secondary)
                        } else {
                            // 用稳定 id（LogLine.id），追加日志时不会整表重建
                            ForEach(backend.logs) { line in
                                Text(line.text)
                                    .appFont(.caption, design: .monospaced)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                            }
                        }
                    }
                    .padding(6)
                }
                .frame(height: 180)
                .background(RoundedRectangle(cornerRadius: 6).fill(Color.secondary.opacity(0.08)))

                HStack {
                    Button(loc.t("清空日志")) { backend.clearLogs() }
                    Button(loc.t("刷新状态")) {
                        Task { await backend.refreshExternalStatus(settings: settings) }
                    }
                }
            }
            .padding(6)
        }
    }

    // MARK: - 动作

    /// 检查清单图标：✓ 已配置 / ⟳ 检测中 / ✗ 未配置
    private var azureSymbol: String {
        switch backend.azureState {
        case .ok: return "checkmark.circle.fill"
        case .probing: return "arrow.triangle.2.circlepath"
        case .missing: return "xmark.circle.fill"
        }
    }

    private var azureColor: Color {
        switch backend.azureState {
        case .ok: return .green
        case .probing: return .secondary
        case .missing: return (backend.azureProbeError?.isEmpty == false) ? .red : .orange
        }
    }

    private func saveSettings() {
        do {
            try settings.persist()
            session.syncAPIClient()
            statusMessage = loc.t("设置已保存（密钥在 Keychain 中）")
        } catch {
            statusMessage = loc.t("保存失败：{0}", error.localizedDescription)
        }
    }

    private func writeDotEnv() {
        do {
            try settings.persist()
            let url = try settings.writeDotEnv()
            statusMessage = loc.t("已写入：{0}", url.path)
        } catch {
            statusMessage = loc.t("写入失败：{0}", error.localizedDescription)
        }
    }

    private func checkDependencies() {
        isCheckingDependencies = true
        dependencyReport = nil
        Task {
            let report = await backend.checkDependencies(settings: settings)
            dependencyReport = report
            isCheckingDependencies = false
        }
    }

    private func chooseProjectDirectory() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        panel.prompt = loc.t("选择包含 main.py 的目录")
        panel.directoryURL = URL(fileURLWithPath: settings.projectDirectory, isDirectory: true)

        if panel.runModal() == .OK, let url = panel.url {
            settings.projectDirectory = url.path
            settings.pythonPath = AppSettings.detectPythonPath(projectDirectory: url.path)
        }
    }
}
