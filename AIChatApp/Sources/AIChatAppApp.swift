import SwiftUI

@main
struct AIChatAppApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var session = SessionStore()

    var body: some Scene {
        WindowGroup(L("本地 AI 对话")) {
            RootView(session: session)
                .environmentObject(session)
                .onAppear {
                    // 退出时要能主动收掉后端进程
                    appDelegate.session = session
                }
        }
        .commands {
            // 这是一个单窗口工具类应用，去掉「新建」菜单项
            CommandGroup(replacing: .newItem) {}

            // 界面字体大小：跟系统 App 一样用 ⌘+ / ⌘− / ⌘0
            CommandMenu(L("显示")) {
                Button(L("放大字体")) {
                    session.settings.increaseFontScale()
                }
                .keyboardShortcut("+", modifiers: .command)

                Button(L("缩小字体")) {
                    session.settings.decreaseFontScale()
                }
                .keyboardShortcut("-", modifiers: .command)

                Divider()

                Button(L("恢复默认字体大小")) {
                    session.settings.resetFontScale()
                }
                .keyboardShortcut("0", modifiers: .command)
            }
        }
    }
}

/// 退出 App 时把后端进程一起收掉（SIGTERM，5 秒不退再 SIGKILL）
@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    var session: SessionStore?

    func applicationWillTerminate(_ notification: Notification) {
        session?.backend.terminateOnAppExit()
    }
}
