import SwiftUI

struct RootView: View {
    @EnvironmentObject private var session: SessionStore
    /// 观察设置：字体档位变化时整棵树要跟着重排
    @ObservedObject private var settings: AppSettings
    /// 用 ⌘+ / ⌘− 调整字号时的临时提示
    @State private var fontScaleHUD: String?
    @State private var hudTask: Task<Void, Never>?

    init(session: SessionStore) {
        self.settings = session.settings
    }

    var body: some View {
        Group {
            if session.isAuthenticated {
                MainView()
            } else {
                LoginView(session: session)
            }
        }
        .appFontScale(CGFloat(settings.fontScale))
        .frame(
            minWidth: CGFloat(900).appScaled(by: CGFloat(settings.fontScale)),
            minHeight: CGFloat(600).appScaled(by: CGFloat(settings.fontScale))
        )
        .overlay(alignment: .bottom) {
            if let fontScaleHUD {
                Text(fontScaleHUD)
                    .appFont(.callout, weight: .medium)
                    .foregroundStyle(.white)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 8)
                    .background(
                        Capsule().fill(Color.black.opacity(0.72))
                    )
                    .padding(.bottom, 28)
                    .transition(.opacity)
                    .allowsHitTesting(false)
            }
        }
        .onChange(of: settings.fontScale) { newValue in
            showFontScaleHUD(for: newValue)
        }
        .task {
            await session.bootstrap()
        }
    }

    /// 改字号时在窗口底部显示「字体大小 130%」，1.2 秒后淡出
    private func showFontScaleHUD(for scale: Double) {
        hudTask?.cancel()
        withAnimation(.easeInOut(duration: 0.15)) {
            fontScaleHUD = "字体大小 \(Int((scale * 100).rounded()))%"
        }
        hudTask = Task { @MainActor in
            try? await Task.sleep(nanoseconds: 1_200_000_000)
            guard !Task.isCancelled else { return }
            withAnimation(.easeInOut(duration: 0.35)) {
                fontScaleHUD = nil
            }
        }
    }
}

struct MainView: View {
    @EnvironmentObject private var session: SessionStore

    var body: some View {
        TabView {
            ChatView(session: session)
                .tabItem { Label("对话", systemImage: "bubble.left.and.bubble.right") }

            SettingsView(session: session)
                .tabItem { Label("设置", systemImage: "gearshape") }
        }
        .padding(.top, 6)
    }
}
