import AppKit
import SwiftUI

// MARK: - 全局界面字体缩放
//
// macOS 上 SwiftUI 的 dynamicTypeSize 不起作用（实测 small…xxxLarge 渲染尺寸完全一样），
// 所以这里自己按比例换算字号：视图里用 .appFont(...) 代替 .font(...)，
// 由根视图通过 .appFontScale(_:) 注入当前档位。

private struct AppFontScaleKey: EnvironmentKey {
    static let defaultValue: CGFloat = 1
}

enum AppTypography {
    /// 正文基准字号（macOS 上是 13pt）
    static var bodyPointSize: CGFloat {
        NSFont.preferredFont(forTextStyle: .body).pointSize
    }

    /// 按比例换算后的系统字体（保留一位小数，避免非整数带来的排印毛刺）
    static func systemFont(
        size: CGFloat,
        weight: Font.Weight = .regular,
        design: Font.Design = .default
    ) -> Font {
        .system(size: max(1, (size * 10).rounded() / 10), weight: weight, design: design)
    }
}

extension EnvironmentValues {
    /// 当前界面缩放比例（1 = 系统标准字号）
    var appFontScale: CGFloat {
        get { self[AppFontScaleKey.self] }
        set { self[AppFontScaleKey.self] = newValue }
    }
}

extension View {
    /// 给整棵视图树设置界面缩放（在 RootView 上调用一次）。
    /// 同时覆盖环境里的默认字体，这样**没写 .font(...) 的文本**（聊天气泡正文、输入框占位符等）
    /// 也会跟着缩放，而不只是显式用了 .appFont(...) 的地方。
    func appFontScale(_ scale: CGFloat) -> some View {
        self
            .environment(\.appFontScale, scale)
            .environment(\.font, AppTypography.systemFont(size: AppTypography.bodyPointSize * scale))
    }

    /// 语义字体 + 缩放，替代 .font(.body) / .font(.caption) 这类写法
    func appFont(
        _ style: Font.TextStyle,
        weight: Font.Weight? = nil,
        design: Font.Design = .default
    ) -> some View {
        modifier(AppFontModifier(style: style, weight: weight, fixedSize: nil, design: design))
    }

    /// 固定字号 + 缩放（图标、箭头等）
    func appFont(
        size: CGFloat,
        weight: Font.Weight = .regular,
        design: Font.Design = .default
    ) -> some View {
        modifier(AppFontModifier(style: nil, weight: weight, fixedSize: size, design: design))
    }
}

private struct AppFontModifier: ViewModifier {
    @Environment(\.appFontScale) private var scale

    let style: Font.TextStyle?
    let weight: Font.Weight?
    let fixedSize: CGFloat?
    let design: Font.Design

    func body(content: Content) -> some View {
        content.font(resolvedFont)
    }

    private var resolvedFont: Font {
        let base = fixedSize ?? Self.basePointSize(for: style ?? .body)
        return AppTypography.systemFont(
            size: base * scale,
            weight: weight ?? Self.defaultWeight(for: style),
            design: design
        )
    }

    /// macOS 上各语义字体的基准点数，直接问 AppKit 拿，避免写死
    private static func basePointSize(for style: Font.TextStyle) -> CGFloat {
        switch style {
        case .largeTitle: return NSFont.preferredFont(forTextStyle: .largeTitle).pointSize
        case .title: return NSFont.preferredFont(forTextStyle: .title1).pointSize
        case .title2: return NSFont.preferredFont(forTextStyle: .title2).pointSize
        case .title3: return NSFont.preferredFont(forTextStyle: .title3).pointSize
        case .headline: return NSFont.preferredFont(forTextStyle: .headline).pointSize
        case .subheadline: return NSFont.preferredFont(forTextStyle: .subheadline).pointSize
        case .body: return NSFont.preferredFont(forTextStyle: .body).pointSize
        case .callout: return NSFont.preferredFont(forTextStyle: .callout).pointSize
        case .footnote: return NSFont.preferredFont(forTextStyle: .footnote).pointSize
        case .caption: return NSFont.preferredFont(forTextStyle: .caption1).pointSize
        case .caption2: return NSFont.preferredFont(forTextStyle: .caption2).pointSize
        default: return NSFont.preferredFont(forTextStyle: .body).pointSize
        }
    }

    private static func defaultWeight(for style: Font.TextStyle?) -> Font.Weight {
        style == .headline ? .semibold : .regular
    }
}

// MARK: - 尺寸缩放

extension CGFloat {
    /// 按界面缩放比例换算固定尺寸（列宽、输入框高度等）
    func appScaled(by scale: CGFloat) -> CGFloat {
        (self * scale).rounded()
    }
}
