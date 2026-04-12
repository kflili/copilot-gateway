// Copilot Gateway menu bar indicator
// Compile: swiftc menubar.swift -o menubar -framework Cocoa
// Usage: launched automatically by gateway.py

import Cocoa

// ─── Log Viewer Panel ────────────────────────────────────────────────────────

class LogViewerController: NSObject, NSWindowDelegate {
    var window: NSWindow!
    var textView: NSTextView!
    var scrollView: NSScrollView!
    var refreshTimer: Timer?
    var gatewayPort: String

    init(port: String) {
        self.gatewayPort = port
        super.init()
    }

    func showWindow() {
        if window != nil {
            window.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            return
        }

        let frame = NSRect(x: 0, y: 0, width: 720, height: 480)
        window = NSWindow(
            contentRect: frame,
            styleMask: [.titled, .closable, .resizable, .miniaturizable],
            backing: .buffered,
            defer: false
        )
        window.title = "⚡️ Gateway Logs"
        window.center()
        window.isReleasedWhenClosed = false
        window.delegate = self
        window.level = .floating
        window.minSize = NSSize(width: 400, height: 200)

        // Dark background scroll view with monospace text
        scrollView = NSScrollView(frame: window.contentView!.bounds)
        scrollView.autoresizingMask = [.width, .height]
        scrollView.hasVerticalScroller = true
        scrollView.hasHorizontalScroller = false
        scrollView.borderType = .noBorder

        textView = NSTextView(frame: scrollView.bounds)
        textView.isEditable = false
        textView.isSelectable = true
        textView.autoresizingMask = [.width]
        textView.backgroundColor = NSColor(calibratedRed: 0.1, green: 0.1, blue: 0.12, alpha: 1.0)
        textView.textColor = NSColor(calibratedRed: 0.85, green: 0.9, blue: 0.85, alpha: 1.0)
        textView.font = NSFont.monospacedSystemFont(ofSize: 11, weight: .regular)
        textView.textContainerInset = NSSize(width: 8, height: 8)
        textView.isHorizontallyResizable = false
        textView.textContainer?.widthTracksTextView = true
        textView.textContainer?.containerSize = NSSize(width: scrollView.bounds.width, height: CGFloat.greatestFiniteMagnitude)

        scrollView.documentView = textView
        window.contentView?.addSubview(scrollView)

        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)

        // Fetch immediately, then every 3 seconds
        fetchLogs()
        refreshTimer = Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { [weak self] _ in
            self?.fetchLogs()
        }
    }

    func fetchLogs() {
        let url = URL(string: "http://localhost:\(gatewayPort)/logs?n=500")!
        let task = URLSession.shared.dataTask(with: url) { [weak self] data, response, error in
            guard let self = self, let data = data,
                  let text = String(data: data, encoding: .utf8) else { return }

            DispatchQueue.main.async {
                let wasAtBottom = self.isScrolledToBottom()
                self.applyColoredLog(text)
                if wasAtBottom {
                    self.scrollToBottom()
                }
            }
        }
        task.resume()
    }

    func applyColoredLog(_ text: String) {
        let storage = NSMutableAttributedString()
        let defaultAttrs: [NSAttributedString.Key: Any] = [
            .font: NSFont.monospacedSystemFont(ofSize: 11, weight: .regular),
            .foregroundColor: NSColor(calibratedRed: 0.85, green: 0.9, blue: 0.85, alpha: 1.0),
        ]

        for line in text.components(separatedBy: "\n") {
            var color = defaultAttrs[.foregroundColor] as! NSColor

            if line.contains("ERROR") || line.contains("error") {
                color = NSColor.systemRed
            } else if line.contains("← 401") || line.contains("← 4") || line.contains("← 5") {
                color = NSColor.systemOrange
            } else if line.contains("POST /v1/messages") || line.contains("POST /chat/completions") || line.contains("POST /v1/responses") {
                color = NSColor.systemCyan
            } else if line.contains("(in=") && line.contains("out=") {
                color = NSColor.systemGreen
            } else if line.contains("streamed") {
                color = NSColor(calibratedRed: 0.6, green: 0.8, blue: 0.6, alpha: 1.0)
            }

            var attrs = defaultAttrs
            attrs[.foregroundColor] = color
            storage.append(NSAttributedString(string: line + "\n", attributes: attrs))
        }

        textView.textStorage?.setAttributedString(storage)
    }

    func isScrolledToBottom() -> Bool {
        let clipView = scrollView.contentView
        let viewHeight = scrollView.documentVisibleRect.height
        let contentHeight = textView.bounds.height
        let scrollY = clipView.bounds.origin.y
        return scrollY + viewHeight >= contentHeight - 20
    }

    func scrollToBottom() {
        textView.scrollToEndOfDocument(nil)
    }

    func windowWillClose(_ notification: Notification) {
        refreshTimer?.invalidate()
        refreshTimer = nil
    }
}

// ─── App Delegate ────────────────────────────────────────────────────────────

class AppDelegate: NSObject, NSApplicationDelegate {
    var statusItem: NSStatusItem!
    var gatewayPort: String = "8787"
    var demoPort: String = "8788"
    var checkTimer: Timer?
    var logViewer: LogViewerController?

    func applicationDidFinishLaunching(_ notification: Notification) {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)

        if let button = statusItem.button {
            button.title = "⚡️CG"
            button.toolTip = "Copilot Gateway"
        }

        let menu = NSMenu()

        let titleItem = NSMenuItem(title: "Copilot Gateway", action: nil, keyEquivalent: "")
        titleItem.isEnabled = false
        menu.addItem(titleItem)
        menu.addItem(NSMenuItem.separator())

        let statusItem = NSMenuItem(title: "● Running", action: nil, keyEquivalent: "")
        statusItem.attributedTitle = NSAttributedString(
            string: "● Running",
            attributes: [.foregroundColor: NSColor.systemGreen, .font: NSFont.monospacedSystemFont(ofSize: 12, weight: .medium)]
        )
        statusItem.tag = 100
        menu.addItem(statusItem)

        // Stats item
        let statsItem = NSMenuItem(title: "📊 0 reqs · 0 tokens", action: nil, keyEquivalent: "")
        statsItem.tag = 200
        menu.addItem(statsItem)

        let uptimeItem = NSMenuItem(title: "   Uptime: 0m", action: nil, keyEquivalent: "")
        uptimeItem.tag = 201
        menu.addItem(uptimeItem)

        menu.addItem(NSMenuItem.separator())

        let demoItem = NSMenuItem(title: "Open Demo UI", action: #selector(openDemo), keyEquivalent: "d")
        demoItem.target = self
        menu.addItem(demoItem)

        let healthItem = NSMenuItem(title: "Check Health", action: #selector(checkHealth), keyEquivalent: "h")
        healthItem.target = self
        menu.addItem(healthItem)

        let logsItem = NSMenuItem(title: "View Logs", action: #selector(openLogViewer), keyEquivalent: "l")
        logsItem.target = self
        menu.addItem(logsItem)

        menu.addItem(NSMenuItem.separator())

        let copyClaudeCmd = NSMenuItem(title: "Copy Claude Code Command", action: #selector(copyClaude), keyEquivalent: "c")
        copyClaudeCmd.target = self
        menu.addItem(copyClaudeCmd)

        menu.addItem(NSMenuItem.separator())

        let stopItem = NSMenuItem(title: "Stop Gateway & Quit", action: #selector(stopAll), keyEquivalent: "q")
        stopItem.target = self
        menu.addItem(stopItem)

        self.statusItem.menu = menu

        // Periodic stats polling (also determines running/stopped status)
        checkTimer = Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { [weak self] _ in
            self?.updateStatus()
        }
        // Initial fetch
        DispatchQueue.main.asyncAfter(deadline: .now() + 2) { [weak self] in
            self?.updateStatus()
        }
    }

    @objc func openDemo() {
        NSWorkspace.shared.open(URL(string: "http://localhost:\(demoPort)")!)
    }

    @objc func checkHealth() {
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/usr/bin/curl")
        task.arguments = ["-s", "http://localhost:\(gatewayPort)/health"]
        let pipe = Pipe()
        task.standardOutput = pipe
        try? task.run()
        task.waitUntilExit()
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        let output = String(data: data, encoding: .utf8) ?? "No response"

        let alert = NSAlert()
        alert.messageText = "Gateway Health"
        alert.informativeText = output
        alert.alertStyle = .informational
        alert.runModal()
    }

    @objc func openLogViewer() {
        if logViewer == nil {
            logViewer = LogViewerController(port: gatewayPort)
        }
        logViewer?.showWindow()
    }

    @objc func copyClaude() {
        let cmd = "ANTHROPIC_AUTH_TOKEN=dummy ANTHROPIC_BASE_URL=http://localhost:8787 claude"
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(cmd, forType: .string)
    }

    @objc func stopAll() {
        // Kill gateway and demo on their ports
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/bin/sh")
        task.arguments = ["-c", "kill $(lsof -ti:8787) $(lsof -ti:8788) 2>/dev/null"]
        try? task.run()
        task.waitUntilExit()
        NSApp.terminate(nil)
    }

    func formatTokens(_ n: Int) -> String {
        if n >= 1_000_000 { return String(format: "%.1fM", Double(n) / 1_000_000) }
        if n >= 1_000 { return String(format: "%.0fK", Double(n) / 1_000) }
        return "\(n)"
    }

    func formatUptime(_ seconds: Int) -> String {
        if seconds >= 3600 {
            let h = seconds / 3600
            let m = (seconds % 3600) / 60
            return "\(h)h \(m)m"
        }
        return "\(seconds / 60)m"
    }

    func updateStatus() {
        let url = URL(string: "http://localhost:\(gatewayPort)/stats")!
        let task = URLSession.shared.dataTask(with: url) { [weak self] data, response, error in
            guard let self = self else { return }
            let httpResp = response as? HTTPURLResponse

            DispatchQueue.main.async {
                let running = httpResp?.statusCode == 200

                // Update title bar
                if let button = self.statusItem.button {
                    if running, let data = data,
                       let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                        let reqs = json["requests_succeeded"] as? Int ?? 0
                        let totalTokens = json["total_tokens"] as? Int ?? 0
                        button.title = "⚡️CG \(reqs)↗ \(self.formatTokens(totalTokens))"
                    } else {
                        button.title = running ? "⚡️CG" : "💤CG"
                    }
                }

                // Update dropdown status
                if let menu = self.statusItem.menu {
                    if let item = menu.item(withTag: 100) {
                        let (text, color) = running
                            ? ("● Running", NSColor.systemGreen)
                            : ("○ Stopped", NSColor.systemGray)
                        item.attributedTitle = NSAttributedString(
                            string: text,
                            attributes: [.foregroundColor: color, .font: NSFont.monospacedSystemFont(ofSize: 12, weight: .medium)]
                        )
                    }

                    if running, let data = data,
                       let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                        let reqs = json["requests_succeeded"] as? Int ?? 0
                        let premium = json["estimated_premium_requests"] as? Double ?? 0
                        let inputT = json["total_input_tokens"] as? Int ?? 0
                        let outputT = json["total_output_tokens"] as? Int ?? 0
                        let uptime = json["uptime_seconds"] as? Int ?? 0

                        if let statsItem = menu.item(withTag: 200) {
                            let premiumStr = premium > 0 ? String(format: " (%.1f premium)", premium) : ""
                            statsItem.title = "📊 \(reqs) reqs\(premiumStr) · \(self.formatTokens(inputT + outputT)) tokens"
                        }
                        if let uptimeItem = menu.item(withTag: 201) {
                            uptimeItem.title = "   \(self.formatTokens(inputT)) in · \(self.formatTokens(outputT)) out · \(self.formatUptime(uptime))"
                        }
                    } else {
                        if let statsItem = menu.item(withTag: 200) {
                            statsItem.title = "📊 —"
                        }
                        if let uptimeItem = menu.item(withTag: 201) {
                            uptimeItem.title = "   —"
                        }
                    }
                }
            }
        }
        task.resume()
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)  // no dock icon, menu bar only
let delegate = AppDelegate()
app.delegate = delegate
app.run()
