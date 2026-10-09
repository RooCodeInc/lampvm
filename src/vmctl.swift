// Runs inside the guest. Screen coordinates are points with a top-left origin.
// Mouse actions use CoreGraphics events; element discovery and verification use Accessibility.
import AppKit
import ApplicationServices
import ImageIO
import Vision

func fail(_ message: String, code: Int32 = 1) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8)); exit(code)
}
func json(_ value: Any) {
    guard let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]),
          let text = String(data: data, encoding: .utf8) else { fail("cannot encode result") }
    print(text)
}
let args = Array(CommandLine.arguments.dropFirst())
let source = CGEventSource(stateID: .hidSystemState)
// Short sleeps in the guest overrun by 10–30 ms each, which made typing and drags several times slower than asked.
// Sleep in small pieces short of the deadline, then wait out the rest.
func pause(_ seconds: Double) {
    let deadline = Date().addingTimeInterval(seconds)
    while deadline.timeIntervalSinceNow > 0.015 { usleep(1_000) }
    while deadline.timeIntervalSinceNow > 0 {}
}
func require(_ count: Int, _ usage: String) { if args.count < count { fail("usage: vmctl " + usage) } }
func number(_ index: Int) -> Double {
    guard args.count > index, let value = Double(args[index]), value.isFinite, abs(value) <= 1_000_000 else { fail("expected finite number between -1000000 and 1000000 at argument \(index)") }
    return value
}
// Required strings are never searched for flags: `type --app` types that text.
// Validate the remaining tokens before touching the desktop, including numeric arity.
let optionArguments: [String] = {
    let command = args.first ?? ""
    let required = ["click":3,"doubleclick":3,"rightclick":3,"move":3,"hit":3,"scroll":4,"drag":5,
                    "type":2,"key":2,"activate":2,"quit":2,"tree":2,"find":3,"clickon":3,"press":3,
                    "setvalue":4,"wait":3,"assert":3,"bounds":2,"windows":2,"ocr":2]
    var start = required[command] ?? 1
    if let count = required[command], args.count < count { fail("not enough arguments for \(command); run vmctl without arguments for usage") }
    if ["scroll","drag","key"].contains(command), args.count > start, !args[start].hasPrefix("--") { start += 1 }
    if command == "display", args.count > 1 {
        guard args.count >= 3 else { fail("display needs both WIDTH and HEIGHT") }; start = 3
    }
    let valueFlags: [String:Set<String>] = [
        "click":["--app"],"doubleclick":["--app"],"rightclick":["--app"],"move":["--app"],
        "scroll":["--app"],"drag":["--app"],"type":["--app"],"key":["--app"],
        "find":["--role"],"clickon":["--role","--index"],"press":["--role","--index"],"setvalue":["--role","--index"],
        "wait":["--role","--timeout"],"assert":["--role","--timeout"]]
    let boolFlags: [String:Set<String>] = [
        "click":["--glide"],"doubleclick":["--glide"],"rightclick":["--glide"],"move":["--glide"],
        "scroll":["--glide"],"drag":["--glide"],
        "tree":["--json"],"find":["--json","--exact"],"clickon":["--exact","--right","--glide"],
        "setvalue":["--exact"],"press":["--exact"],"wait":["--exact","--absent"],"assert":["--exact","--absent"],
        "bounds":["--json"],"trusted":["--prompt"]]
    let options = Array(args.dropFirst(start)); var index = 0; var seen: Set<String> = []
    while index < options.count {
        let flag = options[index]
        guard !seen.contains(flag) else { fail("duplicate option \(flag)") }; seen.insert(flag)
        if valueFlags[command]?.contains(flag) == true {
            guard index + 1 < options.count, !options[index + 1].hasPrefix("--") else { fail("\(flag) needs a value") }; index += 2
        } else if boolFlags[command]?.contains(flag) == true { index += 1 }
        else { fail("unexpected argument \(flag) for \(command)") }
    }
    return options
}()
func has(_ flag: String) -> Bool { optionArguments.contains(flag) }
func option(_ name: String) -> String? {
    guard let index = optionArguments.firstIndex(of: name) else { return nil }
    guard optionArguments.count > index + 1 else { fail("\(name) needs a value") }
    return optionArguments[index + 1]
}
func timeout() -> Double {
    guard let value = Double(option("--timeout") ?? "10"), value.isFinite, value >= 0, value <= 3600 else { fail("timeout must be 0…3600 seconds") }
    return value
}
func trusted() { guard AXIsProcessTrusted() else { fail("Accessibility is not granted to vmctl in the guest; run lampvm setup or grant it in Privacy & Security") } }
func mouse(_ type: CGEventType, _ point: CGPoint, clicks: Int64 = 1, button: CGMouseButton = .left) {
    guard let event = CGEvent(mouseEventSource: source, mouseType: type, mouseCursorPosition: point, mouseButton: button) else { fail("cannot create mouse event") }
    event.setIntegerValueField(.mouseEventClickState, value: clicks); event.post(tap: .cghidEventTap); pause(0.04)
}
// A recording shows the pointer travel to its target instead of appearing there.
// Position follows the clock, so a motion takes the time asked even when the guest sleeps longer than requested.
func travel(_ type: CGEventType, _ from: CGPoint, _ to: CGPoint, seconds: Double, eased: Bool = false) {
    let begun = Date()
    while true {
        let t = min(1, Date().timeIntervalSince(begun) / seconds), fraction = eased ? t * t * (3 - 2 * t) : t
        guard let event = CGEvent(mouseEventSource: source, mouseType: type,
            mouseCursorPosition: CGPoint(x: from.x + (to.x - from.x) * fraction, y: from.y + (to.y - from.y) * fraction), mouseButton: .left) else { fail("cannot create mouse event") }
        event.post(tap: .cghidEventTap); if t >= 1 { break }; pause(0.008)
    }
}
func glide(_ point: CGPoint) {
    guard has("--glide"), let from = CGEvent(source: nil)?.location else { return }
    let distance = hypot(point.x - from.x, point.y - from.y); guard distance > 4 else { return }
    travel(.mouseMoved, from, point, seconds: min(0.35, max(0.12, distance / 2500)), eased: true); pause(0.06)
}
func click(_ point: CGPoint, count: Int = 1, right: Bool = false) {
    glide(point); mouse(.mouseMoved, point); pause(0.12)
    for n in 1...count {
        mouse(right ? .rightMouseDown : .leftMouseDown, point, clicks: Int64(n), button: right ? .right : .left)
        mouse(right ? .rightMouseUp : .leftMouseUp, point, clicks: Int64(n), button: right ? .right : .left)
    }
}
func drag(_ start: CGPoint, _ end: CGPoint, seconds: Double) {
    glide(start); mouse(.mouseMoved, start); mouse(.leftMouseDown, start)
    travel(.leftMouseDragged, start, end, seconds: seconds)
    mouse(.leftMouseUp, end)
}
func scroll(_ point: CGPoint, dy: Int32, dx: Int32) {
    // Adapted from wheel.swift: short pixel-wheel events work with native scroll views.
    glide(point); mouse(.mouseMoved, point); pause(0.08)
    var vertical = dy, horizontal = dx
    while vertical != 0 || horizontal != 0 {
        let y = max(-60, min(60, vertical)), x = max(-60, min(60, horizontal))
        guard let event = CGEvent(scrollWheelEvent2Source: source, units: .pixel, wheelCount: 2, wheel1: y, wheel2: x, wheel3: 0) else { fail("cannot create scroll event") }
        event.post(tap: .cghidEventTap); vertical -= y; horizontal -= x; pause(0.008)
    }
}
let keyCodes: [String: CGKeyCode] = [
    "return":36,"enter":36,"tab":48,"space":49,"delete":51,"backspace":51,"forwarddelete":117,"escape":53,"esc":53,
    "left":123,"right":124,"down":125,"up":126,"home":115,"end":119,"pageup":116,"pagedown":121,
    "a":0,"s":1,"d":2,"f":3,"h":4,"g":5,"z":6,"x":7,"c":8,"v":9,"b":11,"q":12,
    "w":13,"e":14,"r":15,"y":16,"t":17,"o":31,"u":32,"i":34,"p":35,"l":37,"j":38,"k":40,"n":45,"m":46,
    ",":43,".":47,"/":44,";":41,"'":39,"[":33,"]":30,"\\":42,"-":27,"=":24,"`":50,
    "1":18,"2":19,"3":20,"4":21,"5":23,"6":22,"7":26,"8":28,"9":25,"0":29,
    "f1":122,"f2":120,"f3":99,"f4":118,"f5":96,"f6":97,"f7":98,"f8":100,"f9":101,"f10":109,"f11":103,"f12":111
]
func flags(_ names: String) -> CGEventFlags {
    var result: CGEventFlags = []
    for name in names.lowercased().split(separator: ",") {
        switch name { case "cmd","command":result.insert(.maskCommand); case "shift":result.insert(.maskShift)
        case "alt","option","opt":result.insert(.maskAlternate); case "ctrl","control":result.insert(.maskControl)
        default:fail("unknown modifier \(name)") }
    }; return result
}
func key(_ code: CGKeyCode, _ modifiers: CGEventFlags = []) {
    for down in [true,false] {
        guard let event = CGEvent(keyboardEventSource: source, virtualKey: code, keyDown: down) else { fail("cannot create key event") }
        event.flags = modifiers; event.post(tap: .cghidEventTap); pause(0.015)
    }
}
func type(_ text: String) {
    for character in text {
        if character == "\n" { key(36); continue }; if character == "\t" { key(48); continue }
        let units = Array(String(character).utf16)
        for down in [true,false] {
            guard let event = CGEvent(keyboardEventSource: source, virtualKey: 0, keyDown: down) else { fail("cannot create key event") }
            event.flags = []; event.keyboardSetUnicodeString(stringLength: units.count, unicodeString: units)
            event.post(tap: .cghidEventTap); pause(0.012)
        }
    }
}
func attribute<T>(_ element: AXUIElement, _ name: String) -> T? {
    var value: CFTypeRef?
    guard AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success else { return nil }; return value as? T
}
func frame(_ element: AXUIElement) -> CGRect? {
    var position: CFTypeRef?, size: CFTypeRef?
    guard AXUIElementCopyAttributeValue(element,kAXPositionAttribute as CFString,&position) == .success,
          AXUIElementCopyAttributeValue(element,kAXSizeAttribute as CFString,&size) == .success,
          let position, let size, CFGetTypeID(position) == AXValueGetTypeID(), CFGetTypeID(size) == AXValueGetTypeID() else { return nil }
    var point = CGPoint.zero, extent = CGSize.zero
    guard AXValueGetValue(position as! AXValue,.cgPoint,&point), AXValueGetValue(size as! AXValue,.cgSize,&extent) else { return nil }
    return CGRect(origin:point,size:extent)
}
func rectJSON(_ rect: CGRect) -> [String:Double] { ["x":rect.minX,"y":rect.minY,"width":rect.width,"height":rect.height] }
struct Found {
    let element: AXUIElement; let role: String; let label: String; let rect: CGRect?; let path: String
    var object: [String:Any] {
        var object: [String:Any] = ["role":role,"label":label,"path":path]
        if let rect { object["bounds"] = rectJSON(rect) }
        if let enabled: Bool = attribute(element,kAXEnabledAttribute) { object["enabled"] = enabled }
        if let value: NSNumber = attribute(element,kAXValueAttribute) { object["value"] = value }
        else if let value: String = attribute(element,kAXValueAttribute) { object["value"] = value }
        return object
    }
}
func describe(_ element: AXUIElement, path: String) -> Found {
    let role: String = attribute(element,kAXRoleAttribute) ?? "?"
    var parts: [String] = []
    for name in [kAXTitleAttribute,kAXDescriptionAttribute,kAXValueAttribute,kAXPlaceholderValueAttribute,kAXIdentifierAttribute] {
        if let text: String = attribute(element,name), !text.isEmpty, !parts.contains(text) { parts.append(text) }
    }
    return Found(element:element,role:role,label:parts.joined(separator:" | "),rect:frame(element),path:path)
}
func walk(_ element: AXUIElement, depth: Int = 0, path: String = "0", into found: inout [Found]) {
    guard depth < 40, found.count < 20_000 else { return }
    found.append(describe(element,path:path))
    let children: [AXUIElement] = attribute(element,kAXChildrenAttribute) ?? []
    for (index,child) in children.enumerated() { walk(child,depth:depth + 1,path:path + ".\(index)",into:&found) }
}
func running(_ name: String) -> NSRunningApplication? {
    NSWorkspace.shared.runningApplications.first {
        $0.localizedName?.lowercased() == name.lowercased() || $0.bundleIdentifier?.lowercased() == name.lowercased() || String($0.processIdentifier) == name
    }
}
func app(_ name: String) -> NSRunningApplication { guard let app = running(name) else { fail("\(name) is not running") }; return app }
func elements(_ application: NSRunningApplication) -> [Found] {
    var found: [Found] = []; walk(AXUIElementCreateApplication(application.processIdentifier),into:&found); return found
}
func matches(_ items: [Found], _ text: String) -> [Found] {
    let wanted = text.lowercased()
    let filtered = option("--role").map { role in items.filter { $0.role == role } } ?? items
    let exact = filtered.filter { $0.label.lowercased().components(separatedBy:" | ").contains(wanted) }
    if has("--exact") { return exact }
    return exact.isEmpty ? filtered.filter { !$0.label.isEmpty && $0.label.lowercased().contains(wanted) } : exact
}
func target(_ items: [Found]) -> Found {
    let candidates = items.filter { $0.rect != nil && $0.rect!.width > 0 && $0.rect!.height > 0 }
    if let index = option("--index") {
        guard let n = Int(index), n >= 0, n < items.count else { fail("index is outside the matched elements") }
        guard let rect = items[n].rect, rect.width > 0, rect.height > 0 else { fail("indexed element has no visible bounds") }; return items[n]
    }
    let buttons = candidates.filter { $0.role == "AXButton" }
    let preferred = buttons.isEmpty ? candidates : buttons
    guard !preferred.isEmpty else { fail("no matching element with visible bounds") }
    guard preferred.count == 1 else { fail("ambiguous match (\(preferred.count) elements); inspect find --json and choose --index N or --role ROLE") }
    return preferred[0]
}
func show(_ items: [Found]) {
    if has("--json") { json(items.map(\.object)); return }
    for item in items where !item.label.isEmpty || ["AXButton","AXTextField","AXTextArea"].contains(item.role) {
        guard let f = item.rect else { continue }
        print("\(item.role)\t\(Int(f.midX)),\(Int(f.midY))\t\(Int(f.width))x\(Int(f.height))\t\(item.label.replacingOccurrences(of:"\n",with:" "))")
    }
}
// Raising an already-frontmost app can dismiss its transient menu or popover.
func activate(_ application: NSRunningApplication) {
    if NSWorkspace.shared.frontmostApplication?.processIdentifier == application.processIdentifier { return }
    guard application.activate(options:[.activateAllWindows]) else { fail("could not activate app") }
    let ax = AXUIElementCreateApplication(application.processIdentifier)
    if let window: AXUIElement = attribute(ax,kAXFocusedWindowAttribute) { _ = AXUIElementPerformAction(window,kAXRaiseAction as CFString) }
    let deadline = Date().addingTimeInterval(3)
    while NSWorkspace.shared.frontmostApplication?.processIdentifier != application.processIdentifier && Date() < deadline {
        RunLoop.current.run(until:Date().addingTimeInterval(0.05))
    }
    guard NSWorkspace.shared.frontmostApplication?.processIdentifier == application.processIdentifier else { fail("app did not become frontmost") }
    pause(0.15)
}
func activateConfigured() { if let name = option("--app") { activate(app(name)) } }
func hit(_ point: CGPoint) -> AXUIElement? {
    var value: AXUIElement?
    guard AXUIElementCopyElementAtPosition(AXUIElementCreateSystemWide(),Float(point.x),Float(point.y),&value) == .success else { return nil }; return value
}
func pid(_ element: AXUIElement?) -> pid_t? {
    guard let element else { return nil }; var result: pid_t = 0
    return AXUIElementGetPid(element,&result) == .success ? result : nil
}
func hitDiagnostics(_ element: AXUIElement?) -> [String:Any] {
    guard let element else { return ["hit":false] }
    var result = describe(element,path:"hit").object
    result["hit"] = true; result["pid"] = pid(element) ?? -1
    var current: AXUIElement? = attribute(element,kAXParentAttribute), seen: [AXUIElement] = [element]
    var ancestors: [[String:Any]] = []
    while let parent = current, ancestors.count < 40 {
        if seen.contains(where: { CFEqual($0,parent) }) { break }
        seen.append(parent)
        var summary = describe(parent,path:"parent.\(ancestors.count)").object
        summary["pid"] = pid(parent) ?? -1; ancestors.append(summary)
        current = attribute(parent,kAXParentAttribute)
    }
    result["ancestors"] = ancestors
    return result
}
func descends(_ child: AXUIElement, from parent: AXUIElement) -> Bool {
    var current: AXUIElement? = child
    for _ in 0..<40 { guard let element = current else { break }; if CFEqual(element,parent) { return true }; current = attribute(element,kAXParentAttribute) }
    return false
}
func window(_ application: NSRunningApplication) -> AXUIElement {
    let ax = AXUIElementCreateApplication(application.processIdentifier)
    if let window: AXUIElement = attribute(ax,kAXFocusedWindowAttribute), let f = frame(window), f.width > 0, f.height > 0 { return window }
    if let window: AXUIElement = attribute(ax,kAXMainWindowAttribute), let f = frame(window), f.width > 0, f.height > 0 { return window }
    let windows: [AXUIElement] = attribute(ax,kAXWindowsAttribute) ?? []
    guard let window = windows.first(where: { if let f = frame($0) { return f.width > 0 && f.height > 0 }; return false }) else { fail("app has no accessible window") }; return window
}

// Vision runs locally in the guest; OCR has no account, network or desktop permission requirement.
func recognize(_ path: String) {
    let url = URL(fileURLWithPath:path)
    guard let source = CGImageSourceCreateWithURL(url as CFURL,nil),
          let image = CGImageSourceCreateImageAtIndex(source,0,nil) else { fail("cannot read image for OCR: \(path)") }
    let width = image.width, height = image.height
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = true
    request.automaticallyDetectsLanguage = true
    // Use raw image pixels, matching screenshot geometry; do not apply EXIF rotation.
    let handler = VNImageRequestHandler(cgImage:image,orientation:.up,options:[:])
    do { try handler.perform([request]) } catch { fail("OCR failed: \(error.localizedDescription)") }
    let lines: [[String:Any]] = (request.results ?? []).compactMap { observation in
        guard let candidate = observation.topCandidates(1).first else { return nil }
        let box = observation.boundingBox
        return ["text":candidate.string,"confidence":Double(candidate.confidence),
                "bounds":["x":box.minX * Double(width),"y":(1 - box.maxY) * Double(height),
                          "width":box.width * Double(width),"height":box.height * Double(height)]]
    }
    json(["width":width,"height":height,"coordinateSpace":"imagePixelsTopLeft","lines":lines])
}

switch args.first {
case "hit":
    require(3,"hit X Y"); let point = CGPoint(x:number(1),y:number(2)); trusted()
    var result = hitDiagnostics(hit(point)); result["x"] = point.x; result["y"] = point.y; json(result)
    if result["hit"] as? Bool != true { exit(1) }
case "click","doubleclick","rightclick","move":
    require(3,"\(args[0]) X Y [--app APP]"); let point = CGPoint(x:number(1),y:number(2)); trusted(); activateConfigured()
    if args[0] == "move" { glide(point); mouse(.mouseMoved,point) } else { click(point,count:args[0] == "doubleclick" ? 2 : 1,right:args[0] == "rightclick") }
    json(["action":args[0],"x":point.x,"y":point.y,"eventPosted":true])
case "drag":
    require(5,"drag X1 Y1 X2 Y2 [SECONDS] [--app APP]"); let start = CGPoint(x:number(1),y:number(2)), end = CGPoint(x:number(3),y:number(4)); let seconds = args.count > 5 && !args[5].hasPrefix("--") ? number(5) : 0.5
    guard seconds >= 0.05, seconds <= 60 else { fail("drag duration must be 0.05…60 seconds") }
    trusted(); activateConfigured(); drag(start,end,seconds:seconds); json(["action":"drag","eventPosted":true,"seconds":seconds])
case "scroll":
    require(4,"scroll X Y DY [DX] [--app APP]"); let point = CGPoint(x:number(1),y:number(2)); let dy = number(3), dx = args.count > 4 && !args[4].hasPrefix("--") ? number(4) : 0
    guard abs(dy) <= 100_000, abs(dx) <= 100_000 else { fail("scroll delta exceeds 100000 pixels") }
    trusted(); activateConfigured(); scroll(point,dy:Int32(dy),dx:Int32(dx)); json(["action":"scroll","dy":dy,"dx":dx,"eventPosted":true])
case "type": require(2,"type TEXT [--app APP]"); trusted(); activateConfigured(); type(args[1])
case "key":
    require(2,"key NAME [cmd,shift,alt,ctrl] [--app APP]"); trusted(); activateConfigured()
    guard let code = keyCodes[args[1].lowercased()] else { fail("unknown key \(args[1]); use type for arbitrary Unicode") }; key(code,args.count > 2 && !args[2].hasPrefix("--") ? flags(args[2]) : [])
case "activate": require(2,"activate APP"); trusted(); let application = app(args[1]); activate(application); json(["frontmost":true,"pid":application.processIdentifier])
case "quit":
    require(2,"quit APP")
    guard let application = running(args[1]) else { json(["running":false,"requested":false]); exit(0) }
    let requested = application.terminate(), deadline = Date().addingTimeInterval(5)
    while !application.isTerminated && Date() < deadline && requested {
        RunLoop.current.run(until:Date().addingTimeInterval(0.05))
    }
    let terminated = application.isTerminated
    json(["running":!terminated,"requested":requested,"terminated":terminated,"pid":application.processIdentifier])
    if !terminated { fail("app did not quit; inspect its window for an unsaved-changes prompt or refusal") }
case "tree": require(2,"tree APP [--json]"); trusted(); show(elements(app(args[1])))
case "find","clickon","press","setvalue":
    require(args[0] == "setvalue" ? 4 : 3,"\(args[0]) APP TEXT [VALUE] [--exact] [--index N] [--json]"); trusted(); let application = app(args[1])
    if ["clickon","press","setvalue"].contains(args[0]) { activate(application) }
    let found = matches(elements(application),args[2])
    if args[0] == "find" { show(found); exit(found.isEmpty ? 1 : 0) }
    let item = target(found)
    if let enabled: Bool = attribute(item.element,kAXEnabledAttribute), !enabled { fail("matched element is disabled") }
    if args[0] == "setvalue" {
        var settable: DarwinBoolean = false
        guard AXUIElementIsAttributeSettable(item.element,kAXValueAttribute as CFString,&settable) == .success, settable.boolValue else { fail("matched element does not expose an editable value") }
        let result = AXUIElementSetAttributeValue(item.element,kAXValueAttribute as CFString,args[3] as CFString)
        guard result == .success else { fail("setting value failed: \(result.rawValue)") }
        let actual: String = attribute(item.element,kAXValueAttribute) ?? ""
        json(["set":true,"verified":actual == args[3],"role":item.role]); if actual != args[3] { exit(1) }
    } else if args[0] == "press" {
        // An explicit semantic action, distinct from posting a real mouse click.
        let result = AXUIElementPerformAction(item.element,kAXPressAction as CFString)
        guard result == .success else {
            json(["performed":false,"method":"AXPress","errorCode":result.rawValue,"target":item.object])
            fail("Accessibility press failed: \(result.rawValue)")
        }
        json(["performed":true,"method":"AXPress","target":item.object])
    } else {
        let f = item.rect!, point = CGPoint(x:f.midX,y:f.midY), before = hit(point)
        let landed = before.map { pid($0) == application.processIdentifier && descends($0,from:item.element) } ?? false
        guard landed else { json(["landed":false,"eventPosted":false,"reason":"hit-test did not identify the target or its descendant","target":item.object,"hitPID":pid(before) ?? -1,"hit":hitDiagnostics(before)]); exit(1) }
        click(point,right:has("--right")); pause(0.1)
        json(["landed":true,"eventPosted":true,"verification":"pre-click Accessibility hit-test matched target or descendant; semantic result requires a subsequent assertion","target":item.object,"postClickHitPID":pid(hit(point)) ?? -1])
    }
case "wait","assert":
    require(3,"\(args[0]) APP TEXT [--absent] [--timeout SECONDS] [--exact]"); trusted()
    let duration = args[0] == "assert" && !has("--timeout") ? 0 : timeout(), deadline = Date().addingTimeInterval(duration)
    let absent = has("--absent"); var found: [Found] = []
    repeat {
        found = running(args[1]).map { matches(elements($0),args[2]) } ?? []
        if found.isEmpty == absent { json(["ok":true,"absent":absent,"matches":found.count,"text":args[2]]); exit(0) }
        if Date() >= deadline { break }; pause(0.1)
    } while true
    json(["ok":false,"absent":absent,"matches":found.count,"text":args[2],"timeout":duration]); exit(1)
case "bounds","windows":
    require(2,"\(args[0]) APP"); trusted(); let application = app(args[1])
    if args[0] == "bounds" {
        guard let f = frame(window(application)) else { fail("cannot read window bounds") }
        if has("--json") { json(rectJSON(f)) } else { print("\(Int(f.minX.rounded(.down))),\(Int(f.minY.rounded(.down))),\(Int(f.width.rounded(.up))),\(Int(f.height.rounded(.up)))") }
    } else {
        let windows: [AXUIElement] = attribute(AXUIElementCreateApplication(application.processIdentifier),kAXWindowsAttribute) ?? []
        json(windows.enumerated().map { index,window -> [String:Any] in
            var object: [String:Any] = ["index":index,"title":attribute(window,kAXTitleAttribute) as String? ?? ""]
            if let f = frame(window) { object["bounds"] = rectJSON(f) }; return object
        })
    }
case "display":
    let display = CGMainDisplayID(), options = [kCGDisplayShowDuplicateLowResolutionModes:true] as CFDictionary
    let modes = (CGDisplayCopyAllDisplayModes(display,options) as? [CGDisplayMode]) ?? []
    if args.count >= 3 {
        let widthValue = number(1), heightValue = number(2)
        guard widthValue >= 1, widthValue <= 32768, heightValue >= 1, heightValue <= 32768 else { fail("display dimensions must be 1…32768 points") }
        let width = Int(widthValue), height = Int(heightValue)
        guard let mode = modes.filter({ $0.width == width && $0.height == height }).max(by: { $0.pixelWidth < $1.pixelWidth }) else { fail("screen has no \(width)x\(height) mode") }
        var config: CGDisplayConfigRef?
        guard CGBeginDisplayConfiguration(&config) == .success, CGConfigureDisplayWithDisplayMode(config,display,mode,nil) == .success,
              CGCompleteDisplayConfiguration(config,.permanently) == .success else { fail("could not change display mode") }
    }
    if let now = CGDisplayCopyDisplayMode(display) { print("now \(now.width)x\(now.height) points, \(now.pixelWidth)x\(now.pixelHeight) pixels") }
    if args.count < 3 { for mode in Set(modes.map { "\($0.width)x\($0.height) (\($0.pixelWidth)x\($0.pixelHeight) px)" }).sorted() { print(mode) } }
case "apps":
    let applications = NSWorkspace.shared.runningApplications.filter { $0.activationPolicy != .prohibited }
        .sorted { ($0.localizedName ?? "").localizedCaseInsensitiveCompare($1.localizedName ?? "") == .orderedAscending }
    json(applications.map { application -> [String:Any] in
        ["name":application.localizedName ?? "","bundleIdentifier":application.bundleIdentifier as Any? ?? NSNull(),
         "pid":application.processIdentifier,"active":application.isActive,
         "activationPolicy":application.activationPolicy == .regular ? "regular" : "accessory"]
    })
case "ocr": require(2,"ocr IMAGE_PATH"); recognize(args[1])
case "trusted":
    let value = has("--prompt") ? AXIsProcessTrustedWithOptions([kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String:true] as CFDictionary) : AXIsProcessTrusted()
    print(value ? "yes" : "no"); exit(value ? 0 : 1)
// What this helper can do, for a host newer or older than it: 2 added --glide.
case "version": print(2)
default:
    fail("usage: vmctl click|doubleclick|rightclick X Y | move X Y | hit X Y | scroll X Y DY [DX] | drag X1 Y1 X2 Y2 [SECONDS] | type TEXT | key NAME [mods] | activate|quit APP | tree APP [--json] | find|clickon|press APP TEXT | wait|assert APP TEXT [--absent] [--timeout SEC] | setvalue APP TEXT VALUE | bounds|windows APP | apps | ocr IMAGE_PATH | trusted [--prompt] | display [WIDTH HEIGHT]")
}
