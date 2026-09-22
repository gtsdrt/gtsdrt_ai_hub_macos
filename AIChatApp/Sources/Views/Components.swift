import SwiftUI

/// 「左侧标题 + 右侧控件」的一行设置项
struct SettingRow<Content: View>: View {
    @Environment(\.appFontScale) private var fontScale

    let title: String
    @ViewBuilder var content: Content

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Text(title)
                .foregroundStyle(.secondary)
                .frame(width: CGFloat(132).appScaled(by: fontScale), alignment: .leading)
            content
        }
    }
}

/// 登录页用的竖排字段
struct LabeledField<Content: View>: View {
    let title: String
    @ViewBuilder var content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title)
                .appFont(.caption)
                .foregroundStyle(.secondary)
            content
                .textFieldStyle(.roundedBorder)
        }
    }
}

/// 顶部状态小圆点 + 文案
struct StatusBadge: View {
    let text: String
    let isActive: Bool

    var body: some View {
        HStack(spacing: 6) {
            Circle()
                .fill(isActive ? Color.green : Color.orange)
                .frame(width: 8, height: 8)
            Text(text)
                .appFont(.caption)
                .foregroundStyle(.secondary)
        }
    }
}

/// 字段旁边的「?」提示：悬停显示取值路径
struct FieldHelpIcon: View {
    let help: String

    var body: some View {
        Image(systemName: "questionmark.circle")
            .appFont(.caption)
            .foregroundStyle(.secondary)
            .help(help)
    }
}

/// 测试连接的结果行（成功 ✓ 绿 / 失败 ✗ 红 / 需要重启 ⚠ 橙）
struct ConnectionTestResultView: View {
    let state: ConnectionTester.State
    let onRestart: () -> Void

    var body: some View {
        switch state {
        case .idle:
            EmptyView()
        case .running:
            HStack(spacing: 6) {
                ProgressView().controlSize(.small)
                Text("测试中…")
                    .appFont(.caption)
                    .foregroundStyle(.secondary)
            }
        case .success(let message, let details):
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 6) {
                    Image(systemName: "checkmark.circle.fill")
                    Text(message)
                }
                .appFont(.caption)
                .foregroundStyle(.green)

                ForEach(details, id: \.self) { detail in
                    Text("• \(detail)")
                        .appFont(.caption)
                        .foregroundStyle(.secondary)
                        .textSelection(.enabled)
                }
            }
        case .failure(let message):
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Image(systemName: "xmark.circle.fill")
                Text(message)
                    .textSelection(.enabled)
            }
            .appFont(.caption)
            .foregroundStyle(.red)
        case .unavailable(let message):
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Image(systemName: "info.circle.fill")
                Text(message)
                    .textSelection(.enabled)
            }
            .appFont(.caption)
            .foregroundStyle(.orange)
        case .needsRestart(let message):
            HStack(spacing: 8) {
                Image(systemName: "exclamationmark.triangle.fill")
                    .foregroundStyle(.orange)
                Text(message)
                    .appFont(.caption)
                    .foregroundStyle(.orange)
                Button("一键重启并测试", action: onRestart)
                    .appFont(.caption)
            }
        }
    }
}

/// 「测试连接 / 测试余额」按钮：测试中转圈并禁用
struct ConnectionTestButton: View {
    let title: String
    let isRunning: Bool
    var systemImage: String? = nil
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 5) {
                if isRunning {
                    ProgressView().controlSize(.small)
                } else if let systemImage {
                    Image(systemName: systemImage)
                }
                Text(isRunning ? "测试中…" : title)
            }
        }
        .disabled(isRunning)
    }
}
