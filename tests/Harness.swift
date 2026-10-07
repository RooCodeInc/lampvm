import AppKit

// Deliberately offline and deterministic: this window exercises input and observable state changes.
final class DragMarker: NSView {
    override func hitTest(_ point: NSPoint) -> NSView? { nil }
}

final class DropArea: NSView {
    var status: NSTextField!
    var start: NSPoint?
    var current: NSPoint?
    let sourceRect = NSRect(x: 20, y: 22, width: 110, height: 70)
    let targetRect = NSRect(x: 250, y: 22, width: 130, height: 70)
    override var isFlipped: Bool { true }
    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        setAccessibilityElement(true)
        setAccessibilityRole(.group)
        setAccessibilityLabel("Drag playground")
        for (title, rect) in [("Drag source", sourceRect), ("Drop target", targetRect)] {
            let marker = DragMarker(frame: rect)
            marker.setAccessibilityElement(true)
            marker.setAccessibilityRole(.group)
            marker.setAccessibilityLabel(title)
            addSubview(marker)
        }
    }
    required init?(coder: NSCoder) { fatalError() }
    override func draw(_ dirtyRect: NSRect) {
        NSColor.controlBackgroundColor.setFill(); bounds.fill()
        NSColor.systemBlue.withAlphaComponent(0.2).setFill(); sourceRect.fill()
        NSColor.systemGreen.withAlphaComponent(0.2).setFill(); targetRect.fill()
        let attrs: [NSAttributedString.Key: Any] = [.font: NSFont.systemFont(ofSize: 15), .foregroundColor: NSColor.labelColor]
        "Drag source".draw(at: NSPoint(x: 29, y: 48), withAttributes: attrs)
        "Drop target".draw(at: NSPoint(x: 264, y: 48), withAttributes: attrs)
        if let p = current {
            NSColor.systemBlue.setFill()
            NSBezierPath(ovalIn: NSRect(x: p.x-8, y: p.y-8, width: 16, height: 16)).fill()
        }
    }
    override func mouseDown(with event: NSEvent) {
        let p = convert(event.locationInWindow, from: nil)
        start = sourceRect.contains(p) ? p : nil
        current = start
        if start != nil { status.stringValue = "Dragging…" }
        needsDisplay = true
    }
    override func mouseDragged(with event: NSEvent) {
        guard start != nil else { return }
        current = convert(event.locationInWindow, from: nil)
        needsDisplay = true
    }
    override func mouseUp(with event: NSEvent) {
        guard start != nil else { return }
        let p = convert(event.locationInWindow, from: nil)
        current = p; start = nil
        status.stringValue = targetRect.contains(p) ? "Drag landed" : "Drag missed"
        needsDisplay = true
    }
}

final class ContextArea: NSView {
    var status: NSTextField!
    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        setAccessibilityElement(true)
        setAccessibilityRole(.button)
        setAccessibilityLabel("Context target")
    }
    required init?(coder: NSCoder) { fatalError() }
    override func draw(_ dirtyRect: NSRect) {
        NSColor.systemOrange.withAlphaComponent(0.2).setFill(); bounds.fill()
        "Right-click here".draw(at: NSPoint(x: 14, y: 16), withAttributes: [.font: NSFont.systemFont(ofSize: 15), .foregroundColor: NSColor.labelColor])
    }
    override func rightMouseDown(with event: NSEvent) {
        status.stringValue = "Context menu opened"
        let menu = NSMenu(title: "Harness context")
        let item = NSMenuItem(title: "Choose context action", action: #selector(chosen), keyEquivalent: "")
        item.target = self; menu.addItem(item)
        NSMenu.popUpContextMenu(menu, with: event, for: self)
    }
    @objc func chosen() { status.stringValue = "Context action chosen" }
}

final class AppDelegate: NSObject, NSApplicationDelegate, NSTextFieldDelegate {
    var window: NSWindow!
    var clicks = 0
    let clickStatus = NSTextField(labelWithString: "Clicks: 0")
    let textStatus = NSTextField(labelWithString: "Typed: empty")
    let delayed = NSTextField(labelWithString: "Delayed element ready")
    let delayStatus = NSTextField(labelWithString: "Delayed element hidden")
    let dragStatus = NSTextField(labelWithString: "Drag idle")
    let contextStatus = NSTextField(labelWithString: "Context idle")
    let text = NSTextField(string: "")
    let scroll = NSScrollView()
    let scrollStatus = NSTextField(labelWithString: "Scroll offset: 0")
    var scrollObserver: NSObjectProtocol?
    var generation = 0
    func label(_ title: String, x: CGFloat, y: CGFloat, width: CGFloat = 400) -> NSTextField {
        let v = NSTextField(labelWithString: title)
        v.frame = NSRect(x: x, y: y, width: width, height: 24)
        window.contentView!.addSubview(v); return v
    }
    func button(_ title: String, x: CGFloat, y: CGFloat, width: CGFloat, action: Selector) {
        let b = NSButton(title: title, target: self, action: action)
        b.frame = NSRect(x: x, y: y, width: width, height: 32)
        window.contentView!.addSubview(b)
    }
    func add(_ v: NSView, x: CGFloat, y: CGFloat, width: CGFloat, height: CGFloat = 24) {
        v.frame = NSRect(x: x, y: y, width: width, height: height)
        window.contentView!.addSubview(v)
    }
    func applicationDidFinishLaunching(_ notification: Notification) {
        window = NSWindow(contentRect: NSRect(x: 140, y: 160, width: 900, height: 660), styleMask: [.titled, .closable, .miniaturizable], backing: .buffered, defer: false)
        window.title = "LampVM Harness"
        window.isReleasedWhenClosed = false
        _ = label("Offline input test", x: 24, y: 610)
        button("Click Me", x: 24, y: 556, width: 140, action: #selector(clicked))
        add(clickStatus, x: 180, y: 560, width: 250)
        text.placeholderString = "Type here"
        text.setAccessibilityLabel("Type here")
        text.delegate = self
        add(text, x: 24, y: 507, width: 390, height: 30)
        add(textStatus, x: 24, y: 473, width: 430)
        button("Reveal Delayed", x: 24, y: 426, width: 180, action: #selector(reveal))
        button("Hide Delayed", x: 218, y: 426, width: 160, action: #selector(hide))
        add(delayStatus, x: 24, y: 392, width: 420)
        add(delayed, x: 24, y: 366, width: 420); delayed.isHidden = true
        let drag = DropArea(frame: .zero); drag.status = dragStatus
        add(drag, x: 24, y: 215, width: 414, height: 118)
        add(dragStatus, x: 24, y: 185, width: 420)
        let context = ContextArea(frame: .zero); context.status = contextStatus
        add(context, x: 24, y: 104, width: 220, height: 58)
        add(contextStatus, x: 24, y: 70, width: 430)
        button("Reset Harness", x: 24, y: 22, width: 180, action: #selector(reset))
        _ = label("Scroll rows (top → bottom)", x: 474, y: 610)
        add(scrollStatus, x: 474, y: 582, width: 402)
        scroll.frame = NSRect(x: 474, y: 22, width: 402, height: 548)
        scroll.hasVerticalScroller = true
        scroll.borderType = .bezelBorder
        scroll.setAccessibilityLabel("Scroll rows")
        let rows = FlippedView(frame: NSRect(x: 0, y: 0, width: 378, height: 60*34))
        for i in 1...60 {
            let row = NSTextField(labelWithString: String(format: "Row %02d — test content", i))
            row.frame = NSRect(x: 12, y: CGFloat(i-1)*34+6, width: 350, height: 22)
            rows.addSubview(row)
        }
        scroll.documentView = rows; window.contentView!.addSubview(scroll)
        scroll.contentView.postsBoundsChangedNotifications = true
        scrollObserver = NotificationCenter.default.addObserver(forName: NSView.boundsDidChangeNotification, object: scroll.contentView, queue: .main) { [weak self] _ in
            guard let self = self else { return }
            self.scrollStatus.stringValue = "Scroll offset: \(Int(self.scroll.contentView.bounds.origin.y.rounded()))"
        }
        let menu = NSMenu()
        let appMenu = NSMenu(); appMenu.addItem(withTitle: "Quit Harness", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        let appItem = NSMenuItem(); appItem.submenu = appMenu; menu.addItem(appItem)
        let editMenu = NSMenu(title: "Edit")
        for (title, selector, shortcut, modifiers) in [
            ("Undo", Selector(("undo:")), "z", NSEvent.ModifierFlags.command),
            ("Redo", Selector(("redo:")), "z", NSEvent.ModifierFlags([.command, .shift])),
            ("Cut", #selector(NSText.cut(_:)), "x", NSEvent.ModifierFlags.command),
            ("Copy", #selector(NSText.copy(_:)), "c", NSEvent.ModifierFlags.command),
            ("Paste", #selector(NSText.paste(_:)), "v", NSEvent.ModifierFlags.command),
            ("Select All", #selector(NSText.selectAll(_:)), "a", NSEvent.ModifierFlags.command)
        ] {
            let item = NSMenuItem(title: title, action: selector, keyEquivalent: shortcut)
            item.keyEquivalentModifierMask = modifiers
            editMenu.addItem(item)
        }
        let editItem = NSMenuItem(title: "Edit", action: nil, keyEquivalent: "")
        editItem.submenu = editMenu; menu.addItem(editItem)
        NSApp.mainMenu = menu
        window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true)
    }
    @objc func clicked() { clicks += 1; clickStatus.stringValue = "Clicks: \(clicks)" }
    func controlTextDidChange(_ obj: Notification) { textStatus.stringValue = "Typed: \(text.stringValue.isEmpty ? "empty" : text.stringValue)" }
    @objc func reveal() {
        generation += 1; let token = generation
        delayStatus.stringValue = "Waiting for delayed element…"
        DispatchQueue.main.asyncAfter(deadline: .now()+1.5) { [self] in
            guard token == generation else { return }
            delayed.isHidden = false; delayStatus.stringValue = "Delayed element visible"
        }
    }
    @objc func hide() { generation += 1; delayed.isHidden = true; delayStatus.stringValue = "Delayed element hidden" }
    @objc func reset() {
        clicks = 0; clickStatus.stringValue = "Clicks: 0"
        text.stringValue = ""; textStatus.stringValue = "Typed: empty"
        hide(); dragStatus.stringValue = "Drag idle"; contextStatus.stringValue = "Context idle"
        scroll.contentView.scroll(to: .zero); scroll.reflectScrolledClipView(scroll.contentView)
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}
final class FlippedView: NSView { override var isFlipped: Bool { true } }
let app = NSApplication.shared
app.setActivationPolicy(.regular)
let delegate = AppDelegate(); app.delegate = delegate
app.run()
