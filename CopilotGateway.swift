// Copilot Gateway — unified macOS app (menubar + dashboard + gateway launcher)
// Compile: swiftc CopilotGateway.swift -o CopilotGateway.app/Contents/MacOS/CopilotGateway -framework Cocoa
//
// Replaces both launcher.swift and menubar.swift.
// Starts gateway.py, shows menubar status, and provides a dashboard window.

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
                if wasAtBottom { self.scrollToBottom() }
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

// ─── Dashboard Window ────────────────────────────────────────────────────────

class DashboardController: NSObject, NSWindowDelegate {
    var window: NSWindow!
    var refreshTimer: Timer?
    var gatewayPort: String

    // UI elements
    var statusDot: NSTextField!
    var statusLabel: NSTextField!
    var portLabel: NSTextField!
    var uptimeLabel: NSTextField!
    var pidLabel: NSTextField!

    var reqsValue: NSTextField!
    var premiumValue: NSTextField!
    var inputValue: NSTextField!
    var outputValue: NSTextField!
    var totalValue: NSTextField!

    var logTextView: NSTextView!
    var logScrollView: NSScrollView!

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

        let w: CGFloat = 680
        let h: CGFloat = 560
        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: w, height: h),
            styleMask: [.titled, .closable, .resizable, .miniaturizable],
            backing: .buffered,
            defer: false
        )
        window.title = "Copilot Gateway"
        window.center()
        window.isReleasedWhenClosed = false
        window.delegate = self
        window.minSize = NSSize(width: 500, height: 400)
        window.titlebarAppearsTransparent = true
        window.backgroundColor = NSColor(calibratedRed: 0.1, green: 0.1, blue: 0.13, alpha: 1.0)

        let content = NSView(frame: NSRect(x: 0, y: 0, width: w, height: h))
        content.autoresizingMask = [.width, .height]
        window.contentView = content

        var y = h - 50

        // ── Status bar ──
        let statusBar = NSView(frame: NSRect(x: 0, y: y - 10, width: w, height: 50))
        statusBar.autoresizingMask = [.width]

        statusDot = makeLabel("●", size: 16, color: .systemGreen, bold: true)
        statusDot.frame = NSRect(x: 20, y: 14, width: 20, height: 22)
        statusBar.addSubview(statusDot)

        statusLabel = makeLabel("Running", size: 15, color: .white, bold: true)
        statusLabel.frame = NSRect(x: 40, y: 14, width: 80, height: 22)
        statusBar.addSubview(statusLabel)

        portLabel = makeLabel("localhost:\(gatewayPort)", size: 12, color: .secondaryLabelColor)
        portLabel.frame = NSRect(x: 125, y: 15, width: 130, height: 20)
        statusBar.addSubview(portLabel)

        uptimeLabel = makeLabel("", size: 12, color: .secondaryLabelColor)
        uptimeLabel.frame = NSRect(x: 260, y: 15, width: 120, height: 20)
        uptimeLabel.autoresizingMask = []
        statusBar.addSubview(uptimeLabel)

        pidLabel = makeLabel("", size: 12, color: .tertiaryLabelColor)
        pidLabel.frame = NSRect(x: w - 120, y: 15, width: 100, height: 20)
        pidLabel.alignment = .right
        pidLabel.autoresizingMask = [.minXMargin]
        statusBar.addSubview(pidLabel)

        content.addSubview(statusBar)
        y -= 60

        // ── Separator ──
        let sep1 = NSBox(frame: NSRect(x: 20, y: y, width: w - 40, height: 1))
        sep1.boxType = .separator
        sep1.autoresizingMask = [.width]
        content.addSubview(sep1)
        y -= 15

        // ── Stats grid ──
        let statsView = NSView(frame: NSRect(x: 0, y: y - 55, width: w, height: 55))
        statsView.autoresizingMask = [.width]

        let colW: CGFloat = 120
        let startX: CGFloat = 25
        let labelY: CGFloat = 30
        let valueY: CGFloat = 5

        func addStat(_ title: String, col: Int) -> NSTextField {
            let x = startX + CGFloat(col) * colW
            let lbl = makeLabel(title, size: 10, color: .secondaryLabelColor)
            lbl.frame = NSRect(x: x, y: labelY, width: colW, height: 16)
            statsView.addSubview(lbl)
            let val = makeLabel("—", size: 18, color: .white, bold: true)
            val.frame = NSRect(x: x, y: valueY, width: colW, height: 24)
            statsView.addSubview(val)
            return val
        }

        reqsValue = addStat("REQUESTS", col: 0)
        premiumValue = addStat("PREMIUM REQS", col: 1)
        inputValue = addStat("INPUT TOKENS", col: 2)
        outputValue = addStat("OUTPUT TOKENS", col: 3)
        totalValue = addStat("TOTAL TOKENS", col: 4)

        content.addSubview(statsView)
        y -= 70

        // ── Separator ──
        let sep2 = NSBox(frame: NSRect(x: 20, y: y, width: w - 40, height: 1))
        sep2.boxType = .separator
        sep2.autoresizingMask = [.width]
        content.addSubview(sep2)
        y -= 10

        // ── Logs header ──
        let logsLabel = makeLabel("LOGS", size: 10, color: .secondaryLabelColor)
        logsLabel.frame = NSRect(x: 25, y: y - 14, width: 50, height: 14)
        content.addSubview(logsLabel)
        y -= 22

        // ── Log viewer ──
        let logH = y - 55 // leave room for bottom toolbar
        logScrollView = NSScrollView(frame: NSRect(x: 16, y: 55, width: w - 32, height: logH))
        logScrollView.autoresizingMask = [.width, .height]
        logScrollView.hasVerticalScroller = true
        logScrollView.hasHorizontalScroller = false
        logScrollView.borderType = .noBorder
        logScrollView.wantsLayer = true
        logScrollView.layer?.cornerRadius = 8

        logTextView = NSTextView(frame: logScrollView.bounds)
        logTextView.isEditable = false
        logTextView.isSelectable = true
        logTextView.autoresizingMask = [.width]
        logTextView.backgroundColor = NSColor(calibratedRed: 0.07, green: 0.07, blue: 0.09, alpha: 1.0)
        logTextView.textColor = NSColor(calibratedRed: 0.85, green: 0.9, blue: 0.85, alpha: 1.0)
        logTextView.font = NSFont.monospacedSystemFont(ofSize: 11, weight: .regular)
        logTextView.textContainerInset = NSSize(width: 8, height: 8)
        logTextView.isHorizontallyResizable = false
        logTextView.textContainer?.widthTracksTextView = true
        logTextView.textContainer?.containerSize = NSSize(width: logScrollView.bounds.width, height: CGFloat.greatestFiniteMagnitude)

        logScrollView.documentView = logTextView
        content.addSubview(logScrollView)

        // ── Bottom toolbar ──
        let toolbar = NSView(frame: NSRect(x: 0, y: 0, width: w, height: 50))
        toolbar.autoresizingMask = [.width]

        let copyBtn = makeButton("Copy Claude Command", action: #selector(copyClaude), x: 20, y: 12)
        toolbar.addSubview(copyBtn)

        let demoBtn = makeButton("Open Demo UI", action: #selector(openDemo), x: 200, y: 12)
        toolbar.addSubview(demoBtn)

        let stopBtn = makeButton("Stop & Quit", action: #selector(stopAll), x: w - 120, y: 12)
        stopBtn.autoresizingMask = [.minXMargin]
        stopBtn.contentTintColor = .systemRed
        toolbar.addSubview(stopBtn)

        content.addSubview(toolbar)

        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)

        // Start refresh
        fetchAll()
        refreshTimer = Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { [weak self] _ in
            self?.fetchAll()
        }
    }

    func fetchAll() {
        fetchStats()
        fetchLogs()
    }

    func fetchStats() {
        let url = URL(string: "http://localhost:\(gatewayPort)/stats")!
        let task = URLSession.shared.dataTask(with: url) { [weak self] data, response, error in
            guard let self = self else { return }
            let httpResp = response as? HTTPURLResponse
            let running = httpResp?.statusCode == 200

            DispatchQueue.main.async {
                if running, let data = data,
                   let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                    self.statusDot.stringValue = "●"
                    self.statusDot.textColor = .systemGreen
                    self.statusLabel.stringValue = "Running"

                    let reqs = json["requests_succeeded"] as? Int ?? 0
                    let premium = json["estimated_premium_requests"] as? Double ?? 0
                    let inputT = json["total_input_tokens"] as? Int ?? 0
                    let outputT = json["total_output_tokens"] as? Int ?? 0
                    let uptime = json["uptime_seconds"] as? Int ?? 0
                    let pid = json["pid"] as? Int

                    self.reqsValue.stringValue = "\(reqs)"
                    self.premiumValue.stringValue = premium > 0 ? String(format: "%.1f", premium) : "0"
                    self.inputValue.stringValue = self.formatTokens(inputT)
                    self.outputValue.stringValue = self.formatTokens(outputT)
                    self.totalValue.stringValue = self.formatTokens(inputT + outputT)
                    self.uptimeLabel.stringValue = "↑ \(self.formatUptime(uptime))"
                    if let pid = pid { self.pidLabel.stringValue = "PID \(pid)" }
                } else {
                    self.statusDot.stringValue = "○"
                    self.statusDot.textColor = .systemGray
                    self.statusLabel.stringValue = "Stopped"
                    self.uptimeLabel.stringValue = ""
                    self.pidLabel.stringValue = ""
                    for v in [self.reqsValue, self.premiumValue, self.inputValue, self.outputValue, self.totalValue] {
                        v?.stringValue = "—"
                    }
                }
            }
        }
        task.resume()
    }

    func fetchLogs() {
        let url = URL(string: "http://localhost:\(gatewayPort)/logs?n=200")!
        let task = URLSession.shared.dataTask(with: url) { [weak self] data, response, error in
            guard let self = self, let data = data,
                  let text = String(data: data, encoding: .utf8) else { return }
            DispatchQueue.main.async {
                let wasAtBottom = self.isScrolledToBottom()
                self.applyColoredLog(text)
                if wasAtBottom { self.scrollToBottom() }
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
        logTextView.textStorage?.setAttributedString(storage)
    }

    func isScrolledToBottom() -> Bool {
        let viewHeight = logScrollView.documentVisibleRect.height
        let contentHeight = logTextView.bounds.height
        let scrollY = logScrollView.contentView.bounds.origin.y
        return scrollY + viewHeight >= contentHeight - 20
    }

    func scrollToBottom() {
        logTextView.scrollToEndOfDocument(nil)
    }

    @objc func copyClaude() {
        let cmd = "ANTHROPIC_AUTH_TOKEN=dummy ANTHROPIC_BASE_URL=http://localhost:\(gatewayPort) claude"
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(cmd, forType: .string)
        // Brief visual feedback
        let original = (NSApp.delegate as? AppDelegate)?.statusItem.button?.title
        (NSApp.delegate as? AppDelegate)?.statusItem.button?.title = "⚡️ Copied!"
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) {
            (NSApp.delegate as? AppDelegate)?.statusItem.button?.title = original ?? "⚡️CG"
        }
    }

    @objc func openDemo() {
        let demoPort = (NSApp.delegate as? AppDelegate)?.demoPort ?? "8788"
        NSWorkspace.shared.open(URL(string: "http://localhost:\(demoPort)")!)
    }

    @objc func stopAll() {
        (NSApp.delegate as? AppDelegate)?.stopAll()
    }

    func windowWillClose(_ notification: Notification) {
        refreshTimer?.invalidate()
        refreshTimer = nil
    }

    // ── Helpers ──

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

    func makeLabel(_ text: String, size: CGFloat, color: NSColor, bold: Bool = false) -> NSTextField {
        let label = NSTextField(labelWithString: text)
        label.font = bold ? NSFont.systemFont(ofSize: size, weight: .semibold) : NSFont.systemFont(ofSize: size)
        label.textColor = color
        label.backgroundColor = .clear
        label.isBezeled = false
        label.isEditable = false
        return label
    }

    func makeButton(_ title: String, action: Selector, x: CGFloat, y: CGFloat) -> NSButton {
        let btn = NSButton(title: title, target: self, action: action)
        btn.bezelStyle = .rounded
        btn.frame = NSRect(x: x, y: y, width: 160, height: 28)
        btn.font = NSFont.systemFont(ofSize: 12)
        return btn
    }
}

// ─── App Delegate ────────────────────────────────────────────────────────────

class AppDelegate: NSObject, NSApplicationDelegate {
    var statusItem: NSStatusItem!
    var gatewayPort: String = "8787"
    var demoPort: String = "8788"
    var checkTimer: Timer?
    var logViewer: LogViewerController?
    var dashboard: DashboardController?
    var gatewayProcess: Process?
    var gatewayVersion: String?

    func applicationDidFinishLaunching(_ notification: Notification) {
        gatewayPort = ProcessInfo.processInfo.environment["GATEWAY_PORT"] ?? "8787"

        // ── Start gateway.py ──
        startGateway()

        // ── Menubar ──
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)

        if let button = statusItem.button {
            button.title = "⚡️CG"
            button.toolTip = "Copilot Gateway"
        }

        let menu = NSMenu()

        let titleItem = NSMenuItem(title: "Copilot Gateway", action: nil, keyEquivalent: "")
        titleItem.isEnabled = false
        titleItem.tag = 300
        menu.addItem(titleItem)
        menu.addItem(NSMenuItem.separator())

        let statusMenuItem = NSMenuItem(title: "● Running", action: nil, keyEquivalent: "")
        statusMenuItem.attributedTitle = NSAttributedString(
            string: "● Starting…",
            attributes: [.foregroundColor: NSColor.systemYellow, .font: NSFont.monospacedSystemFont(ofSize: 12, weight: .medium)]
        )
        statusMenuItem.tag = 100
        menu.addItem(statusMenuItem)

        let statsItem = NSMenuItem(title: "📊 0 reqs · 0 tokens", action: nil, keyEquivalent: "")
        statsItem.tag = 200
        menu.addItem(statsItem)

        let uptimeItem = NSMenuItem(title: "   Uptime: 0m", action: nil, keyEquivalent: "")
        uptimeItem.tag = 201
        menu.addItem(uptimeItem)

        menu.addItem(NSMenuItem.separator())

        let dashItem = NSMenuItem(title: "Open Dashboard", action: #selector(showDashboard), keyEquivalent: "d")
        dashItem.target = self
        menu.addItem(dashItem)

        let demoItem = NSMenuItem(title: "Open Demo UI", action: #selector(openDemo), keyEquivalent: "")
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

        // Periodic stats polling
        checkTimer = Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { [weak self] _ in
            self?.updateStatus()
        }
        // Initial fetch after gateway has a moment to start
        DispatchQueue.main.asyncAfter(deadline: .now() + 3) { [weak self] in
            self?.updateStatus()
            self?.fetchVersion()
        }
    }

    // ── Gateway lifecycle ──

    func startGateway() {
        // Find gateway.py relative to .app bundle
        let appURL = Bundle.main.bundleURL.resolvingSymlinksInPath()
        let gatewayDir = appURL.deletingLastPathComponent()
        let gatewayPy = gatewayDir.appendingPathComponent("gateway.py").path

        guard FileManager.default.fileExists(atPath: gatewayPy) else {
            DispatchQueue.main.asyncAfter(deadline: .now() + 1) {
                let alert = NSAlert()
                alert.messageText = "gateway.py not found"
                alert.informativeText = "Expected at: \(gatewayPy)\n\nMake sure CopilotGateway.app is in the same folder as gateway.py."
                alert.alertStyle = .critical
                alert.runModal()
                NSApp.terminate(nil)
            }
            return
        }

        // Find python3
        var pythonPath: String?
        for candidate in ["/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"] {
            if FileManager.default.isExecutableFile(atPath: candidate) {
                pythonPath = candidate
                break
            }
        }
        guard let python = pythonPath else {
            DispatchQueue.main.asyncAfter(deadline: .now() + 1) {
                let alert = NSAlert()
                alert.messageText = "python3 not found"
                alert.informativeText = "Install Python 3 to use Copilot Gateway."
                alert.alertStyle = .critical
                alert.runModal()
                NSApp.terminate(nil)
            }
            return
        }

        // Inherit user PATH so gateway.py can find `gh`
        if let shell = ProcessInfo.processInfo.environment["SHELL"] ?? Optional("/bin/zsh") {
            let pathProc = Process()
            pathProc.executableURL = URL(fileURLWithPath: shell)
            pathProc.arguments = ["-l", "-c", "echo $PATH"]
            let pipe = Pipe()
            pathProc.standardOutput = pipe
            pathProc.standardError = FileHandle.nullDevice
            try? pathProc.run()
            pathProc.waitUntilExit()
            let pathData = pipe.fileHandleForReading.readDataToEndOfFile()
            if let userPath = String(data: pathData, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines),
               !userPath.isEmpty {
                setenv("PATH", userPath, 1)
            }
        }

        // Check if already running
        let checkProc = Process()
        checkProc.executableURL = URL(fileURLWithPath: "/usr/bin/curl")
        checkProc.arguments = ["-s", "--connect-timeout", "1", "http://localhost:\(gatewayPort)/health"]
        checkProc.standardOutput = FileHandle.nullDevice
        checkProc.standardError = FileHandle.nullDevice
        try? checkProc.run()
        checkProc.waitUntilExit()
        if checkProc.terminationStatus == 0 {
            // Already running — just attach to it
            return
        }

        // Start gateway.py (without menubar — we ARE the menubar now)
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: python)
        proc.arguments = [gatewayPy, "--port", gatewayPort]
        proc.currentDirectoryURL = gatewayDir
        proc.environment = ProcessInfo.processInfo.environment
        // Tell gateway.py not to launch its own menubar
        proc.environment?["GATEWAY_NO_MENUBAR"] = "1"

        let logFile = "/tmp/copilot-gateway.log"
        FileManager.default.createFile(atPath: logFile, contents: nil)
        if let logHandle = FileHandle(forWritingAtPath: logFile) {
            logHandle.seekToEndOfFile()
            proc.standardOutput = logHandle
            proc.standardError = logHandle
        }
        proc.standardInput = FileHandle.nullDevice

        do {
            try proc.run()
            gatewayProcess = proc
        } catch {
            DispatchQueue.main.asyncAfter(deadline: .now() + 1) {
                let alert = NSAlert()
                alert.messageText = "Failed to start gateway"
                alert.informativeText = error.localizedDescription
                alert.alertStyle = .critical
                alert.runModal()
            }
        }
    }

    // ── Click Dock icon → show dashboard ──

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showDashboard()
        return true
    }

    @objc func showDashboard() {
        if dashboard == nil {
            dashboard = DashboardController(port: gatewayPort)
        }
        dashboard?.showWindow()
        if let version = gatewayVersion {
            dashboard?.window?.title = "Copilot Gateway v\(version)"
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
        let cmd = "ANTHROPIC_AUTH_TOKEN=dummy ANTHROPIC_BASE_URL=http://localhost:\(gatewayPort) claude"
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(cmd, forType: .string)
    }

    @objc func stopAll() {
        // Kill gateway process
        if let proc = gatewayProcess, proc.isRunning {
            proc.terminate()
        }
        // Also kill anything on the ports (catches the case where we attached to existing)
        let killTask = Process()
        killTask.executableURL = URL(fileURLWithPath: "/bin/sh")
        killTask.arguments = ["-c", "kill $(lsof -ti:\(gatewayPort)) $(lsof -ti:\(demoPort)) 2>/dev/null"]
        try? killTask.run()
        killTask.waitUntilExit()
        NSApp.terminate(nil)
    }

    func applicationWillTerminate(_ notification: Notification) {
        if let proc = gatewayProcess, proc.isRunning {
            proc.terminate()
        }
    }

    // ── Menubar status updates (same as original menubar.swift) ──

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

    func fetchVersion() {
        let url = URL(string: "http://localhost:\(gatewayPort)/health")!
        let task = URLSession.shared.dataTask(with: url) { [weak self] data, _, _ in
            guard let self = self, let data = data,
                  let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let version = json["version"] as? String else { return }
            DispatchQueue.main.async {
                self.gatewayVersion = version
                // Update menubar dropdown title
                if let menu = self.statusItem.menu, let titleItem = menu.item(withTag: 300) {
                    titleItem.title = "Copilot Gateway v\(version)"
                }
                // Update dashboard window title
                if let win = self.dashboard?.window {
                    win.title = "Copilot Gateway v\(version)"
                }
            }
        }
        task.resume()
    }

    func updateStatus() {
        let url = URL(string: "http://localhost:\(gatewayPort)/stats")!
        let task = URLSession.shared.dataTask(with: url) { [weak self] data, response, error in
            guard let self = self else { return }
            let httpResp = response as? HTTPURLResponse

            DispatchQueue.main.async {
                let running = httpResp?.statusCode == 200

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
                        if let statsItem = menu.item(withTag: 200) { statsItem.title = "📊 —" }
                        if let uptimeItem = menu.item(withTag: 201) { uptimeItem.title = "   —" }
                    }
                }
            }
        }
        task.resume()
    }
}

// ─── Main ────────────────────────────────────────────────────────────────────

// Kill stale menubar processes from old standalone menubar binary
let myPid = ProcessInfo.processInfo.processIdentifier
let pgrep = Process()
pgrep.executableURL = URL(fileURLWithPath: "/usr/bin/pgrep")
pgrep.arguments = ["-x", "menubar"]
let pgrepPipe = Pipe()
pgrep.standardOutput = pgrepPipe
try? pgrep.run()
pgrep.waitUntilExit()
let pgrepOutput = String(data: pgrepPipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
for line in pgrepOutput.components(separatedBy: "\n") {
    if let pid = Int32(line.trimmingCharacters(in: .whitespaces)), pid != myPid {
        kill(pid, SIGTERM)
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.regular)  // show in Dock
let delegate = AppDelegate()
app.delegate = delegate
app.run()
