import AppKit
import SwiftUI

struct LoginView: View {
    @ObservedObject var session: SessionStore
    @StateObject private var checklist: FirstRunChecklist
    @Environment(\.appFontScale) private var fontScale

    @State private var username = ""
    @State private var password = ""
    @State private var rememberPassword = true
    /// 非 nil 时弹出设置页，并滚动到对应区域
    @State private var settingsTarget: SettingsSection?

    init(session: SessionStore) {
        self.session = session
        _checklist = StateObject(wrappedValue: FirstRunChecklist(session: session))
    }

    var body: some View {
        VStack(spacing: 18) {
            VStack(spacing: 6) {
                Image(systemName: "bubble.left.and.bubble.right.fill")
                    .appFont(size: 34)
                    .foregroundStyle(.tint)
                Text("本地 AI 对话")
                    .appFont(.title2)
                    .bold()
                Text(session.settings.backendBaseURL)
                    .appFont(.caption)
                    .foregroundStyle(.secondary)
            }

            Button {
                Task { await session.loginWithGoogle() }
            } label: {
                HStack(spacing: 8) {
                    Image(systemName: "person.crop.circle.badge.checkmark")
                    Text(session.isLoggingIn ? "等待 Google 登录…" : "使用 Google 账号登录")
                }
                .frame(width: CGFloat(280).appScaled(by: fontScale))
            }
            .controlSize(.large)
            .disabled(session.isLoggingIn)

            Button {
                Task { await session.loginWithGitHub() }
            } label: {
                HStack(spacing: 8) {
                    Image(systemName: "chevron.left.forwardslash.chevron.right")
                    Text(session.isLoggingIn ? "等待 GitHub 登录…" : "使用 GitHub 账号登录")
                }
                .frame(width: CGFloat(280).appScaled(by: fontScale))
            }
            .controlSize(.large)
            .disabled(session.isLoggingIn)

            HStack {
                Rectangle().frame(height: 1).foregroundStyle(.quaternary)
                Text("或使用本地管理员（应急入口）")
                    .appFont(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize()
                Rectangle().frame(height: 1).foregroundStyle(.quaternary)
            }
            .frame(width: CGFloat(360).appScaled(by: fontScale))

            VStack(alignment: .leading, spacing: 12) {
                LabeledField(title: "用户名") {
                    TextField("admin", text: $username)
                }
                LabeledField(title: "密码") {
                    SecureField("password123", text: $password)
                }
                Toggle("记住密码（保存在 Keychain）", isOn: $rememberPassword)
                    .appFont(.caption)
            }
            .frame(width: CGFloat(320).appScaled(by: fontScale))

            HStack(spacing: 12) {
                Button {
                    Task {
                        await session.login(
                            username: username.trimmingCharacters(in: .whitespaces),
                            password: password,
                            rememberPassword: rememberPassword
                        )
                    }
                } label: {
                    if session.isLoggingIn {
                        ProgressView().controlSize(.small)
                    } else {
                        Text("登录")
                    }
                }
                .keyboardShortcut(.defaultAction)
                .disabled(session.isLoggingIn || username.isEmpty || password.isEmpty)

                Button("退出") {
                    NSApplication.shared.terminate(nil)
                }
            }

            if let error = session.loginError {
                Text(error)
                    .appFont(.callout)
                    .foregroundStyle(.red)
                    .frame(width: CGFloat(360).appScaled(by: fontScale))
                    .multilineTextAlignment(.center)
            }
            if let notice = session.notice {
                Text(notice)
                    .appFont(.callout)
                    .foregroundStyle(.secondary)
            }

            Divider()
                .frame(width: CGFloat(320).appScaled(by: fontScale))

            FirstRunChecklistView(
                checklist: checklist,
                onOpenSettings: { target in
                    settingsTarget = target
                },
                onCollapse: {
                    checklist.stop()
                }
            )

            backendRow
        }
        .padding(40)
        .onAppear {
            username = session.settings.username
            password = session.settings.loginPassword
        }
        .task {
            checklist.start()
        }
        .onDisappear {
            checklist.stop()
        }
        .sheet(item: $settingsTarget, onDismiss: {
            // 从设置页返回登录页：立刻重查一次
            checklist.refresh()
        }) { target in
            SettingsView(
                session: session,
                scrollTarget: target,
                onClose: { settingsTarget = nil }
            )
            .frame(minWidth: 780, minHeight: 560)
        }
    }

    private var backendRow: some View {
        HStack(spacing: 10) {
            StatusBadge(text: "本地后端：\(session.backend.status.text)", isActive: session.backend.status.isRunning)

            if !session.backend.status.isRunning {
                Button("启动后端") {
                    session.backend.start(settings: session.settings)
                }
                .controlSize(.small)
            }
        }
    }
}
