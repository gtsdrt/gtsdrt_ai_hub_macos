import AppKit
import Sparkle

// Initialize the actual packaged framework with the packaged Info.plist. Do not
// enter an event loop: this checks configuration without showing UI or fetching.
guard CommandLine.arguments.count == 2,
      let bundle = Bundle(path: CommandLine.arguments[1]) else {
    fputs("Supply the packaged app bundle\n", stderr)
    exit(1)
}
let driver = SPUStandardUserDriver(hostBundle: bundle, delegate: nil)
let updater = SPUUpdater(hostBundle: bundle, applicationBundle: bundle,
                         userDriver: driver, delegate: nil)
do {
    try updater.start()
    print("Sparkle updater configuration valid; interval=\(updater.updateCheckInterval)s")
} catch {
    fputs("Cannot start packaged Sparkle updater: \(error.localizedDescription)\n", stderr)
    exit(1)
}
