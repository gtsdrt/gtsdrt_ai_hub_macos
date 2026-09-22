// 生成占位 App 图标：深蓝渐变背景 + 白色 "AI"
//
// 用法（项目根目录）：
//   swiftc -O scripts/make_icons.swift -o /tmp/make_icons && /tmp/make_icons
//
// 产物：
//   Assets.xcassets/AppIcon.appiconset/*.png（16…1024，含 @2x）
//
// 说明：这里只生成 PNG 交给 Assets.xcassets（Xcode 会编译成 Assets.car 并写 CFBundleIconName）。
// 不再额外生成 .icns —— 本机 macOS 26 的 iconutil 连系统自带 iconset 往返都报 Invalid Iconset，
// 而且有了 AppIcon.appiconset 也不需要 icns。

import AppKit
import Foundation

let projectRoot = URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
let appIconSet = projectRoot.appendingPathComponent("Assets.xcassets/AppIcon.appiconset")

/// AppIcon.appiconset 需要的全部图片：文件名 → 像素尺寸
let variants: [(name: String, size: Int)] = [
    ("icon_16x16.png", 16),
    ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32),
    ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128),
    ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256),
    ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512),
    ("icon_512x512@2x.png", 1024),
]

func renderIcon(pixels: Int) -> Data? {
    let size = CGFloat(pixels)
    guard let space = CGColorSpace(name: CGColorSpace.sRGB),
          let context = CGContext(
              data: nil,
              width: pixels,
              height: pixels,
              bitsPerComponent: 8,
              bytesPerRow: 0,
              space: space,
              bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
          ) else {
        return nil
    }

    // macOS 风格：留一点边距 + 圆角
    let margin = size * 0.06
    let body = CGRect(x: margin, y: margin, width: size - margin * 2, height: size - margin * 2)
    let radius = body.width * 0.225
    context.addPath(CGPath(roundedRect: body, cornerWidth: radius, cornerHeight: radius, transform: nil))
    context.clip()

    // 深蓝渐变（左上深、右下亮）
    let top = NSColor(srgbRed: 0.04, green: 0.08, blue: 0.20, alpha: 1).cgColor
    let bottom = NSColor(srgbRed: 0.10, green: 0.32, blue: 0.70, alpha: 1).cgColor
    if let gradient = CGGradient(
        colorsSpace: space,
        colors: [top, bottom] as CFArray,
        locations: [0, 1]
    ) {
        context.drawLinearGradient(
            gradient,
            start: CGPoint(x: 0, y: size),
            end: CGPoint(x: size, y: 0),
            options: []
        )
    }

    // 白色 "AI" 居中
    let previous = NSGraphicsContext.current
    NSGraphicsContext.current = NSGraphicsContext(cgContext: context, flipped: false)
    let fontSize = size * 0.40
    let attributes: [NSAttributedString.Key: Any] = [
        .font: NSFont.systemFont(ofSize: fontSize, weight: .bold),
        .foregroundColor: NSColor.white,
    ]
    let text = NSAttributedString(string: "AI", attributes: attributes)
    let textSize = text.size()
    text.draw(at: NSPoint(
        x: (size - textSize.width) / 2,
        y: (size - textSize.height) / 2 + size * 0.015
    ))
    NSGraphicsContext.current = previous

    guard let image = context.makeImage() else { return nil }
    return NSBitmapImageRep(cgImage: image).representation(using: .png, properties: [:])
}

let fileManager = FileManager.default
try? fileManager.createDirectory(at: appIconSet, withIntermediateDirectories: true)

var written = 0
for variant in variants {
    guard let data = renderIcon(pixels: variant.size) else {
        FileHandle.standardError.write(Data("渲染失败：\(variant.name)\n".utf8))
        exit(1)
    }
    try data.write(to: appIconSet.appendingPathComponent(variant.name))
    written += 1
}

print("已生成 \(written) 个 PNG：\(appIconSet.path)")
