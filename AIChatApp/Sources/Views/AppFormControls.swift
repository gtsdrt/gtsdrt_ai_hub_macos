import AppKit
import SwiftUI

extension View {
    /// Native rounded fields keep a fixed bezel height when only their font grows.
    /// Use a plain native editor inside a border sized from the actual font metrics.
    func appTextField() -> some View {
        modifier(AppTextFieldModifier())
    }
}

private struct AppTextFieldModifier: ViewModifier {
    @Environment(\.appFontScale) private var fontScale
    @Environment(\.isEnabled) private var isEnabled
    @FocusState private var isFocused: Bool

    private var lineHeight: CGFloat {
        let size = (AppTypography.bodyPointSize * fontScale * 10).rounded() / 10
        let font = NSFont.systemFont(ofSize: size)
        return ceil(NSLayoutManager().defaultLineHeight(for: font))
    }

    func body(content: Content) -> some View {
        content
            .textFieldStyle(.plain)
            .appFont(.body)
            .focused($isFocused)
            .frame(height: lineHeight)
            .padding(.horizontal, max(6, 6 * fontScale))
            .padding(.vertical, max(3, 3 * fontScale))
            .background(Color(nsColor: .textBackgroundColor), in: RoundedRectangle(cornerRadius: 5))
            .overlay {
                RoundedRectangle(cornerRadius: 5)
                    .strokeBorder(isFocused ? Color.accentColor : Color(nsColor: .separatorColor),
                                  lineWidth: isFocused ? 2 : 1)
                    .allowsHitTesting(false)
            }
            .opacity(isEnabled ? 1 : 0.5)
    }
}

/// Keep labels readable; stack the editor underneath when the window is narrow.
struct SettingRow<Content: View>: View {
    @Environment(\.appFontScale) private var fontScale

    let title: String
    @ViewBuilder var content: Content

    var body: some View {
        ViewThatFits(in: .horizontal) {
            HStack(alignment: .firstTextBaseline, spacing: 10) {
                label
                    .frame(width: CGFloat(190).appScaled(by: fontScale), alignment: .leading)
                content
                    .frame(minWidth: CGFloat(160).appScaled(by: fontScale), maxWidth: .infinity,
                           alignment: .leading)
            }
            VStack(alignment: .leading, spacing: 6) {
                label
                content
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }

    private var label: some View {
        Text(title)
            .foregroundStyle(.secondary)
            .fixedSize(horizontal: false, vertical: true)
    }
}

/// Login fields use the same scalable editor as Settings.
struct LabeledField<Content: View>: View {
    let title: String
    @ViewBuilder var content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title)
                .appFont(.caption)
                .foregroundStyle(.secondary)
            content.appTextField()
        }
    }
}
