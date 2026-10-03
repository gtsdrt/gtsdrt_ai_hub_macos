import AppKit
import SwiftUI
import UniformTypeIdentifiers

struct BackupSettingsSection: View {
    @StateObject private var controller: BackupController
    @State private var form: BackupFormMode?
    @Environment(\.loc) private var loc

    init(session: SessionStore) { _controller = StateObject(wrappedValue: BackupController(session: session)) }

    var body: some View {
        SettingsSectionBox(title: loc.t("备份与恢复")) {
            VStack(alignment: .leading, spacing: 10) {
                Text(loc.t("用独立密码加密聊天、任务、已保存设置和容器配置，可选包含钥匙串凭据与日志。请先保存设置。"))
                    .appFont(.callout)
                HStack {
                    Button(loc.t("创建加密备份…")) { controller.clearPreview(); form = .create }
                    Button(loc.t("从备份恢复…")) {
                        let panel = NSOpenPanel()
                        panel.title = loc.t("选择加密备份")
                        panel.allowedContentTypes = [.backupArchive]
                        panel.allowsMultipleSelection = false
                        if panel.runModal() == .OK, let url = panel.url {
                            controller.clearPreview(); form = .restore(url)
                        }
                    }
                }
                .disabled(controller.isBusy)
                Text(loc.t("备份密码丢失后无法恢复。备份不包含开发目录、.env、更新签名私钥或缓存。"))
                    .appFont(.caption).foregroundStyle(.secondary)
                if let message = controller.message {
                    Text(message).appFont(.callout).textSelection(.enabled)
                }
                if let url = controller.recoveryURL {
                    Button(loc.t("显示恢复前备份")) { NSWorkspace.shared.activateFileViewerSelecting([url]) }
                }
            }
            .padding(6)
        }
        .sheet(item: $form, onDismiss: { controller.clearPreview(clearMessage: false) }) { mode in
            BackupForm(controller: controller, mode: mode)
        }
    }
}

private enum BackupFormMode: Identifiable {
    case create, restore(URL)
    var id: String {
        switch self { case .create: return "create"; case .restore(let url): return url.path }
    }
}

private extension UTType {
    static let backupArchive = UTType(exportedAs: "com.example.AIChatApp.backup", conformingTo: .data)
}

private struct BackupForm: View {
    @ObservedObject var controller: BackupController
    let mode: BackupFormMode
    @Environment(\.dismiss) private var dismiss
    @Environment(\.loc) private var loc
    @Environment(\.appFontScale) private var scale
    @State private var password = ""
    @State private var confirmation = ""
    @State private var includeCredentials = false
    @State private var includeLogs = false
    @State private var restoreDatabase = true
    @State private var restoreSettings = true
    @State private var restoreCredentials = false
    @State private var restoreLogs = false
    @State private var confirmingRestore = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                Text(loc.t(isCreating ? "创建加密备份…" : "从备份恢复…")).appFont(.title2, weight: .semibold)
                LabeledField(title: loc.t("备份密码")) { SecureField(loc.t("至少 12 个字符"), text: $password) }
                    .disabled(controller.prepared != nil)
                if isCreating {
                    LabeledField(title: loc.t("再次输入备份密码")) { SecureField("", text: $confirmation) }
                    Toggle(loc.t("包含钥匙串密码和 API Key"), isOn: $includeCredentials)
                    Toggle(loc.t("包含后端日志"), isOn: $includeLogs)
                    Text(loc.t("密码不会保存在备份文件中。请将备份密码保存在安全的位置。"))
                        .appFont(.caption).foregroundStyle(.secondary)
                } else if let payload = controller.prepared, let summary = controller.summary {
                    Text(loc.t("备份时间：{0}", payload.createdAt.formatted(date: .abbreviated, time: .shortened)))
                    Text(loc.t("来源版本：{0}；会话：{1}；任务：{2}", payload.appVersion,
                               String(summary.conversations), String(summary.tasks)))
                    Toggle(loc.t("恢复聊天与任务（替换当前记录）"), isOn: $restoreDatabase).disabled(payload.database == nil)
                    Toggle(loc.t("恢复已保存设置和容器配置"), isOn: $restoreSettings)
                    if payload.credentials != nil {
                        Toggle(loc.t("恢复钥匙串凭据（覆盖同名条目）"), isOn: $restoreCredentials)
                    }
                    if payload.log != nil { Toggle(loc.t("恢复后端日志"), isOn: $restoreLogs) }
                    Text(loc.t("恢复会暂停后端并要求重新登录。恢复前自动生成加密备份，使用本次输入的同一密码。"))
                        .appFont(.caption).foregroundStyle(.secondary)
                }
                if controller.isBusy { ProgressView(loc.t("正在处理备份…")) }
                if let message = controller.message { Text(message).foregroundStyle(.secondary).textSelection(.enabled) }
                HStack {
                    Button(loc.t("取消")) { password = ""; confirmation = ""; dismiss() }
                        .keyboardShortcut(.cancelAction)
                    Spacer()
                    if isCreating {
                        Button(loc.t("保存加密备份…")) { save() }
                            .disabled(password.count < 12 || password != confirmation || password.utf8.count > 1024)
                    } else if controller.prepared == nil {
                        Button(loc.t("验证并预览")) {
                            if case .restore(let url) = mode {
                                Task { await controller.inspect(url, password: password) }
                            }
                        }.disabled(password.isEmpty)
                    } else {
                        Button(loc.t("恢复所选数据…")) { confirmingRestore = true }
                            .disabled(!restoreDatabase && !restoreSettings && !restoreCredentials && !restoreLogs)
                    }
                }
            }
            .padding(24)
            .disabled(controller.isBusy)
        }
        .frame(width: min(640 * scale, 900), height: min(500 * scale, 760))
        .interactiveDismissDisabled(controller.isBusy)
        .alert(loc.t("确认恢复所选数据？"), isPresented: $confirmingRestore) {
            Button(loc.t("取消"), role: .cancel) {}
            Button(loc.t("恢复"), role: .destructive) {
                let selection = BackupRepository.Selection(database: restoreDatabase, settings: restoreSettings,
                    credentials: restoreCredentials, logs: restoreLogs)
                Task {
                    if await controller.restore(password: password, selection: selection) {
                        password = ""; dismiss()
                    }
                }
            }
        } message: {
            Text(loc.t("选中的数据将覆盖当前数据，正在执行的请求会中断。恢复前备份可用于撤销本次恢复。"))
        }
        .onChange(of: controller.prepared != nil) { ready in
            if ready {
                restoreDatabase = controller.prepared?.database != nil
                restoreCredentials = controller.prepared?.credentials != nil
            }
        }
    }

    private var isCreating: Bool { if case .create = mode { return true }; return false }

    private func save() {
        let panel = NSSavePanel()
        panel.title = loc.t("保存加密备份…")
        panel.allowedContentTypes = [.backupArchive]
        panel.canCreateDirectories = true
        panel.nameFieldStringValue = "AIChatApp-\(Int(Date().timeIntervalSince1970)).aichatbackup"
        guard panel.runModal() == .OK, let url = panel.url else { return }
        Task {
            if await controller.create(to: url, password: password,
                                       includeCredentials: includeCredentials, includeLogs: includeLogs) {
                password = ""; confirmation = ""; dismiss()
            }
        }
    }
}
