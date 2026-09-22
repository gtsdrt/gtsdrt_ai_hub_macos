import SwiftUI

/// 登录页底部的首次运行检查清单（默认折叠）
struct FirstRunChecklistView: View {
    @ObservedObject var checklist: FirstRunChecklist
    /// 点「去配置」：带上要跳转的区域
    let onOpenSettings: (SettingsSection) -> Void
    /// 用户收起清单：调用方停止轮询
    let onCollapse: () -> Void

    @State private var isExpanded = false
    @State private var isHidden = false
    @State private var showingReadyBanner = false
    @State private var hideTask: Task<Void, Never>?
    @Environment(\.appFontScale) private var fontScale

    var body: some View {
        Group {
            if isHidden {
                EmptyView()
            } else if showingReadyBanner {
                readyBanner
            } else {
                toggleRow
                if isExpanded {
                    itemList
                }
            }
        }
        .onChange(of: checklist.isReady) { isReady in
            handleReadiness(isReady)
        }
        .onDisappear {
            hideTask?.cancel()
        }
    }

    // MARK: - 折叠行

    private var toggleRow: some View {
        Button {
            withAnimation(.easeInOut(duration: 0.2)) {
                isExpanded.toggle()
            }
            if !isExpanded {
                onCollapse()
            }
        } label: {
            HStack(spacing: 4) {
                Text("首次使用？点这里检查配置")
                Image(systemName: isExpanded ? "chevron.down" : "chevron.right")
                    .appFont(size: 9, weight: .semibold)
            }
            .appFont(.caption)
            .foregroundStyle(.secondary)
        }
        .buttonStyle(.plain)
    }

    // MARK: - 展开后的检查项

    private var itemList: some View {
        VStack(alignment: .leading, spacing: 7) {
            ForEach(FirstRunChecklist.Item.allCases) { item in
                HStack(spacing: 8) {
                    icon(for: checklist.status(for: item))

                    Text(item.title)
                        .appFont(.caption)
                        .frame(width: CGFloat(88).appScaled(by: fontScale), alignment: .leading)

                    Text(checklist.status(for: item).text)
                        .appFont(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                        .frame(maxWidth: .infinity, alignment: .leading)

                    Button("去配置") {
                        onOpenSettings(item.section)
                    }
                    .appFont(.caption)
                    .buttonStyle(.link)
                }
            }
        }
        .padding(12)
        .frame(width: CGFloat(360).appScaled(by: fontScale), alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: 8)
                .fill(Color.secondary.opacity(0.08))
        )
    }

    @ViewBuilder
    private func icon(for status: FirstRunChecklist.Status) -> some View {
        switch status {
        case .checking:
            Image(systemName: "arrow.triangle.2.circlepath")
                .foregroundStyle(.secondary)
        case .ok:
            Image(systemName: "checkmark.circle.fill")
                .foregroundStyle(.green)
        case .failed:
            Image(systemName: "xmark.circle.fill")
                .foregroundStyle(.red)
        case .warning:
            Image(systemName: "exclamationmark.triangle.fill")
                .foregroundStyle(.orange)
        case .optional:
            Image(systemName: "minus.circle")
                .foregroundStyle(.secondary)
        }
    }

    // MARK: - 全部就绪

    private var readyBanner: some View {
        HStack(spacing: 6) {
            Image(systemName: "checkmark.circle.fill")
            Text(
                checklist.hasWarnings
                    ? "✓ 可以登录了（存储为 memory，重启会丢历史）"
                    : "✓ 配置完整，可以登录了"
            )
        }
        .appFont(.callout)
        .foregroundStyle(checklist.hasWarnings ? Color.orange : Color.green)
        .transition(.opacity)
    }

    private func handleReadiness(_ isReady: Bool) {
        guard isReady else {
            // 之前已经因为就绪隐藏了，现在又不就绪（比如后端停了）→ 重新显示
            if isHidden {
                isHidden = false
                showingReadyBanner = false
            }
            return
        }

        isExpanded = false
        withAnimation(.easeInOut(duration: 0.25)) {
            showingReadyBanner = true
        }

        hideTask?.cancel()
        hideTask = Task { @MainActor in
            try? await Task.sleep(nanoseconds: 3_000_000_000)
            guard !Task.isCancelled else { return }
            withAnimation(.easeInOut(duration: 0.4)) {
                showingReadyBanner = false
                isHidden = true
            }
        }
    }
}
