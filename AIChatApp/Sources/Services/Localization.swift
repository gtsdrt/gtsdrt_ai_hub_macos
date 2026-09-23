import Foundation
import SwiftUI

/// 运行时的界面语言。
///
/// 源码里直接写中文原文当 key，查表换成英文 / 挪威语；缺译文就原样显示中文，
/// 所以没覆盖到的字符串不会变成空白。
///
/// - 视图：`@Environment(\.loc) private var loc`，语言一变 SwiftUI 会自动重绘
/// - 服务层 / ViewModel 等非视图代码：`Localizer.current`（或全局函数 `L(...)`）
struct Localizer: Equatable {
    /// 非视图代码用的当前语言。只在主线程写（`AppSettings.language.didSet`），
    /// 其它线程最多读到上一帧的语言，不会崩。
    nonisolated(unsafe) static var current = Localizer(language: .system)

    /// 已解析过的语言（不会是 `.system`）
    let language: AppLanguage

    init(language: AppLanguage) {
        self.language = language.resolved
    }

    /// 取译文
    func t(_ chinese: String) -> String {
        switch language {
        case .english:
            return LocalizationTableEN.table[chinese] ?? chinese
        case .norwegian:
            return LocalizationTableNB.table[chinese] ?? chinese
        case .system, .simplifiedChinese:
            return chinese
        }
    }

    /// 带占位符的译文：源码里写 `{0}` / `{1}`，按顺序替换。
    /// 不用 `String(format:)`，免得 `%` 和 CVarArg 类型把界面搞崩。
    func t(_ chinese: String, _ arguments: String...) -> String {
        formatted(chinese, arguments)
    }

    /// 数组版本（转发用，变参不能直接传给变参）
    func formatted(_ chinese: String, _ arguments: [String]) -> String {
        var text = t(chinese)
        for (index, value) in arguments.enumerated() {
            text = text.replacingOccurrences(of: "{\(index)}", with: value)
        }
        return text
    }
}

/// 非视图代码（服务层 / ViewModel）用的快捷入口
func L(_ chinese: String) -> String { Localizer.current.t(chinese) }
func L(_ chinese: String, _ arguments: String...) -> String {
    Localizer.current.formatted(chinese, arguments)
}

private struct LocalizerKey: EnvironmentKey {
    static let defaultValue = Localizer(language: .system)
}

extension EnvironmentValues {
    /// 当前界面语言
    var loc: Localizer {
        get { self[LocalizerKey.self] }
        set { self[LocalizerKey.self] = newValue }
    }
}

extension View {
    /// 给整棵视图树指定界面语言（在 RootView 上调用一次）
    func appLanguage(_ language: AppLanguage) -> some View {
        environment(\.loc, Localizer(language: language))
    }
}
