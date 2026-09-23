import Foundation

/// 界面语言。
///
/// `system` = 跟随 macOS 的首选语言（在受支持的三种里挑第一个，认不出来就用英文）。
enum AppLanguage: String, CaseIterable, Identifiable, Hashable {
    case system
    case simplifiedChinese = "zh-Hans"
    case english = "en"
    case norwegian = "nb"

    var id: String { rawValue }

    /// 设置页里选项的排列顺序（中文 → English → Norsk → 跟随系统）
    static let pickerOrder: [AppLanguage] = [.simplifiedChinese, .english, .norwegian, .system]

    /// 设置页里选项的名字：三种语言各写自己的名字（跟 macOS 的语言列表一致）。
    /// 「跟随系统」返回中文原文，由调用方用 `loc.t(...)` 翻成当前界面语言。
    var displayName: String {
        switch self {
        case .system:
            return "跟随系统"
        case .simplifiedChinese:
            return "中文"
        case .english:
            return "English"
        case .norwegian:
            return "Norsk"
        }
    }

    /// 真正生效的语言：把 `.system` 解析成系统首选语言里第一个受支持的
    var resolved: AppLanguage {
        guard self == .system else { return self }
        for identifier in Locale.preferredLanguages {
            let tag = identifier.lowercased()
            if tag.hasPrefix("zh") { return .simplifiedChinese }
            if tag.hasPrefix("nb") || tag.hasPrefix("nn") || tag.hasPrefix("no") { return .norwegian }
            if tag.hasPrefix("en") { return .english }
        }
        return .english
    }
}
