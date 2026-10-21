// Runs on the host for `lampvm video`: turns a recording into a movie worth watching.
// Usage: videoedit IN OUT [--timeline FILE] [--condense SECONDS] [--crop X,Y,W,H] [--width N] [--max-speed N] [--hold SECONDS]
//
// Stretches where the picture stands still for at least --condense seconds are played fast, with a badge saying how much
// time passed; the first --hold seconds of each are kept at real speed so the viewer can read what the screen reached.
// A timeline (the marks `record --mark` wrote) is drawn as a caption from each mark to the next, in a band added under
// the picture so it covers nothing; a long one wraps to three lines and is shortened with "…" past that.
// --crop cuts to a part of the picture, in the movie's pixels; --width scales the result. Nothing needs ffmpeg.
import AVFoundation
import AppKit
import QuartzCore

func fail(_ message: String) -> Never { FileHandle.standardError.write(Data((message + "\n").utf8)); exit(1) }
let args = Array(CommandLine.arguments.dropFirst())
guard args.count >= 2 else { fail("usage: videoedit IN OUT [--timeline FILE] [--condense SECONDS] [--crop X,Y,W,H] [--width N] [--max-speed N] [--hold SECONDS]") }
var options: [String: String] = [:]
var index = 2
while index < args.count {
    guard args[index].hasPrefix("--"), index + 1 < args.count else { fail("unexpected argument \(args[index])") }
    options[args[index]] = args[index + 1]; index += 2
}
func number(_ flag: String, _ fallback: Double) -> Double {
    guard let text = options[flag] else { return fallback }
    guard let value = Double(text), value.isFinite, value >= 0 else { fail("\(flag) needs a number") }
    return value
}
let input = URL(fileURLWithPath: args[0]), output = URL(fileURLWithPath: args[1])
guard !FileManager.default.fileExists(atPath: output.path) else { fail("output already exists; choose a new filename") }
let condense = number("--condense", 0)        // 0: no fast-forwarding
let hold = number("--hold", 1.0)
let maxSpeed = max(1, number("--max-speed", 30))
let fastSeconds = 1.5                         // how long a condensed stretch plays, unless --max-speed makes it longer
// A frame is still while fewer than this many pixels, at an eighth of the picture's size, have moved since the stretch
// began: about 3000 of the picture's own, less than a short line of text. A clock tick, a spinner, a line that breathes
// or the pointer stay under it; a message arriving or ten characters typed go over it, whatever the crop's size.
let stillPixels = 48
var marks: [(Double, String)] = []
if let path = options["--timeline"] {
    guard let data = FileManager.default.contents(atPath: path), let list = try? JSONSerialization.jsonObject(with: data) as? [[String: Any]] else { fail("cannot read the timeline \(path)") }
    marks = list.compactMap { entry in
        guard let seconds = entry["seconds"] as? Double, let text = entry["text"] as? String else { return nil }
        return (seconds, text)
    }.sorted { $0.0 < $1.0 }
}

// The source.
let asset = AVURLAsset(url: input)
let semaphore = DispatchSemaphore(value: 0)
var tracks: [AVAssetTrack] = [], duration = CMTime.zero, naturalSize = CGSize.zero, preferredTransform = CGAffineTransform.identity
Task {
    tracks = (try? await asset.loadTracks(withMediaType: .video)) ?? []
    duration = (try? await asset.load(.duration)) ?? .zero
    if let track = tracks.first {
        naturalSize = (try? await track.load(.naturalSize)) ?? .zero
        preferredTransform = (try? await track.load(.preferredTransform)) ?? .identity
    }
    semaphore.signal()
}
semaphore.wait()
guard let track = tracks.first, naturalSize.width > 0, duration.seconds > 0 else { fail("cannot read a video track from \(input.path)") }
let full = CGRect(origin: .zero, size: naturalSize)
var crop = full
if let text = options["--crop"] {
    let parts = text.split(separator: ",").compactMap { Double($0) }
    guard parts.count == 4, parts[2] > 0, parts[3] > 0 else { fail("--crop needs X,Y,WIDTH,HEIGHT in movie pixels") }
    crop = CGRect(x: parts[0], y: parts[1], width: parts[2], height: parts[3]).intersection(full)
    guard crop.width > 0, crop.height > 0 else { fail("--crop lies outside the picture, which is \(Int(full.width))x\(Int(full.height))") }
}
let outputWidth = options["--width"].map { Double($0) ?? 0 } ?? crop.width
guard outputWidth > 0 else { fail("--width needs a number") }
let scale = min(1, outputWidth / crop.width)
let renderSize = CGSize(width: (crop.width * scale / 2).rounded(.down) * 2, height: (crop.height * scale / 2).rounded(.down) * 2)

func composition(for source: AVAsset, track: AVAssetTrack, size: CGSize, scale: Double) -> AVMutableVideoComposition {
    let video = AVMutableVideoComposition()
    video.renderSize = size
    video.frameDuration = CMTime(value: 1, timescale: 30)
    let instruction = AVMutableVideoCompositionInstruction()
    instruction.timeRange = CMTimeRange(start: .zero, duration: CMTime(seconds: 36000, preferredTimescale: 600))
    let layer = AVMutableVideoCompositionLayerInstruction(assetTrack: track)
    layer.setTransform(preferredTransform.concatenating(CGAffineTransform(translationX: -crop.minX, y: -crop.minY)).concatenating(CGAffineTransform(scaleX: scale, y: scale)), at: .zero)
    instruction.layerInstructions = [layer]
    video.instructions = [instruction]
    return video
}

// Where the picture stands still: each frame is compared with the frame that began the current stretch, so slow
// progress such as typing or streaming text breaks a stretch once it adds up, while a clock tick or a cursor does not.
// A mark ends a stretch too, so what it names is shown at real speed for the hold.
struct Stretch { let start: Double; let end: Double }
var stills: [Stretch] = []
if condense > 0 {
    let analysisScale = 0.125
    let analysisSize = CGSize(width: max(2, (crop.width * analysisScale / 2).rounded(.down) * 2), height: max(2, (crop.height * analysisScale / 2).rounded(.down) * 2))
    guard let reader = try? AVAssetReader(asset: asset) else { fail("cannot read \(input.path)") }
    let frames = AVAssetReaderVideoCompositionOutput(videoTracks: [track], videoSettings: [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA])
    frames.videoComposition = composition(for: asset, track: track, size: analysisSize, scale: analysisScale)
    frames.alwaysCopiesSampleData = false
    reader.add(frames)
    guard reader.startReading() else { fail("cannot read \(input.path): \(reader.error?.localizedDescription ?? "unknown error")") }
    func pixels(_ sample: CMSampleBuffer) -> [UInt8]? {
        guard let buffer = CMSampleBufferGetImageBuffer(sample) else { return nil }
        CVPixelBufferLockBaseAddress(buffer, .readOnly); defer { CVPixelBufferUnlockBaseAddress(buffer, .readOnly) }
        guard let base = CVPixelBufferGetBaseAddress(buffer) else { return nil }
        let width = CVPixelBufferGetWidth(buffer), height = CVPixelBufferGetHeight(buffer), stride = CVPixelBufferGetBytesPerRow(buffer)
        var out = [UInt8](repeating: 0, count: width * height)
        for y in 0..<height {
            let row = base.advanced(by: y * stride).assumingMemoryBound(to: UInt8.self)
            for x in 0..<width { out[y * width + x] = row[x * 4 + 1] }  // green, near enough to brightness
        }
        return out
    }
    var reference: [UInt8]? = nil, stillStart = 0.0
    var lastTime = 0.0
    func changed(_ a: [UInt8], _ b: [UInt8]) -> Int {
        guard a.count == b.count, !a.isEmpty else { return Int.max }
        var moved = 0
        for i in 0..<a.count where abs(Int(a[i]) - Int(b[i])) > 24 { moved += 1 }
        return moved
    }
    var pendingMarks = marks.map { $0.0 }
    while let sample = frames.copyNextSampleBuffer() {
        let time = CMTimeGetSeconds(sample.presentationTimeStamp)
        lastTime = time
        guard let now = pixels(sample) else { continue }
        guard let ref = reference else { reference = now; stillStart = time; continue }
        var marked = false
        while let next = pendingMarks.first, next <= time { pendingMarks.removeFirst(); marked = true }
        if marked || changed(now, ref) >= stillPixels {
            if time - stillStart >= condense { stills.append(Stretch(start: stillStart, end: time)) }
            reference = now; stillStart = time
        }
    }
    guard reader.status == .completed else { fail("the movie could not be read to the end: \(reader.error?.localizedDescription ?? "unknown error")") }
    let end = max(lastTime, duration.seconds)
    if end - stillStart >= condense { stills.append(Stretch(start: stillStart, end: end)) }
}

// The cut: real time, then each still stretch after its hold played fast.
struct Segment { let from: Double; let to: Double; let fast: Bool; var outDuration: Double }
var segments: [Segment] = []
var cursor = 0.0
let total = duration.seconds
for still in stills {
    let fastFrom = still.start + hold, fastTo = still.end
    guard fastTo - fastFrom > 0.5 else { continue }
    if fastFrom > cursor { segments.append(Segment(from: cursor, to: fastFrom, fast: false, outDuration: fastFrom - cursor)) }
    let length = fastTo - fastFrom
    segments.append(Segment(from: fastFrom, to: fastTo, fast: true, outDuration: min(length, max(fastSeconds, length / maxSpeed))))
    cursor = fastTo
}
if total > cursor { segments.append(Segment(from: cursor, to: total, fast: false, outDuration: total - cursor)) }

let edit = AVMutableComposition()
guard let editTrack = edit.addMutableTrack(withMediaType: .video, preferredTrackID: kCMPersistentTrackID_Invalid) else { fail("cannot build the movie") }
var outCursor = CMTime.zero
let timescale: CMTimeScale = 600
var outStarts: [Double] = []
for segment in segments {
    let range = CMTimeRange(start: CMTime(seconds: segment.from, preferredTimescale: timescale), end: CMTime(seconds: segment.to, preferredTimescale: timescale))
    do { try editTrack.insertTimeRange(range, of: track, at: outCursor) } catch { fail("cannot cut the movie: \(error.localizedDescription)") }
    outStarts.append(outCursor.seconds)
    if segment.fast {
        editTrack.scaleTimeRange(CMTimeRange(start: outCursor, duration: range.duration), toDuration: CMTime(seconds: segment.outDuration, preferredTimescale: timescale))
    }
    outCursor = CMTimeAdd(outCursor, CMTime(seconds: segment.outDuration, preferredTimescale: timescale))
}
let outTotal = outCursor.seconds
// A moment of the recording, in the finished movie.
func outTime(_ seconds: Double) -> Double {
    for (i, segment) in segments.enumerated() where seconds <= segment.to || i == segments.count - 1 {
        let within = max(0, min(seconds, segment.to) - segment.from)
        let length = max(segment.to - segment.from, 0.001)
        return outStarts[i] + within / length * segment.outDuration
    }
    return outTotal
}

func tidy(_ value: Double) -> Double { (value * 10).rounded() / 10 }
// Captions: the marks in a band under the picture, the badges over its top right corner.
// A sixtieth of the width, at least 14, unless the movie is so narrow that 14 would leave no room for a word.
let fontSize = max(1, min(14, (renderSize.width / 16).rounded(.down)), (renderSize.width / 60).rounded())
let margin = fontSize * 0.9
let pad = CGSize(width: fontSize * 0.8, height: fontSize * 0.45)
let captionLines = 3
// The widest text a caption may have, so that its box and the margins either side stay inside the movie.
let textWidth = max(1, renderSize.width - margin * 2 - pad.width * 2)
func styled(_ text: String, badge: Bool) -> NSAttributedString {
    let paragraph = NSMutableParagraphStyle()
    paragraph.lineBreakMode = .byWordWrapping
    let font = NSFont.systemFont(ofSize: fontSize, weight: badge ? .semibold : .medium)
    return NSAttributedString(string: text, attributes: [.font: font, .foregroundColor: NSColor.white, .paragraphStyle: paragraph])
}
func measure(_ text: NSAttributedString) -> CGSize {
    let box = text.boundingRect(with: CGSize(width: textWidth, height: .greatestFiniteMagnitude), options: [.usesLineFragmentOrigin, .usesFontLeading])
    return CGSize(width: min(textWidth, box.width.rounded(.up)), height: box.height.rounded(.up))
}
// The text as drawn: wrapped to the width, and when it needs more than three lines, cut at a word with "…" so it takes three.
func fitted(_ text: String, badge: Bool = false) -> (text: NSAttributedString, size: CGSize, shortened: Bool) {
    let flat = text.split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
    let limit = measure(styled(Array(repeating: "Ag", count: captionLines).joined(separator: "\n"), badge: badge)).height + 1
    let whole = styled(flat, badge: badge)
    if measure(whole).height <= limit { return (whole, measure(whole), false) }
    // The longest beginning that fits, found by halving: first in whole words, then in characters of the word that
    // overflows, so a long address keeps as much of itself as the lines hold.
    let words = flat.split(separator: " ").map(String.init)
    func cut(_ start: String) -> NSAttributedString {
        styled(start.trimmingCharacters(in: CharacterSet.punctuationCharacters.union(.whitespaces)) + "…", badge: badge)
    }
    func longest(_ count: Int, _ start: (Int) -> String) -> Int {
        var low = 0, high = count
        while low < high {
            let middle = (low + high + 1) / 2
            if measure(cut(start(middle))).height <= limit { low = middle } else { high = middle - 1 }
        }
        return low
    }
    let kept = longest(words.count - 1) { words.prefix($0).joined(separator: " ") }
    let lead = words.prefix(kept).joined(separator: " ") + (kept > 0 ? " " : "")
    let next = words[kept]
    // An ordinary word is left out whole; only one as wide as half a line is cut inside.
    let long = kept == 0 || measure(styled(next, badge: badge)).width >= textWidth / 2
    let short = cut(lead + (long ? next.prefix(max(1, longest(next.count - 1) { lead + next.prefix($0) })) : ""))
    return (short, measure(short), true)
}
// The band holds the tallest caption with a margin above and below; without marks the movie has no band.
let fittedMarks = marks.map { fitted($0.1) }
let band = fittedMarks.isEmpty ? 0 : ((fittedMarks.map { $0.size.height }.max()! + pad.height * 2 + margin * 2) / 2).rounded(.up) * 2
let pictureSize = renderSize
let movieSize = CGSize(width: pictureSize.width, height: pictureSize.height + band)
let video = composition(for: edit, track: editTrack, size: movieSize, scale: scale)
func caption(_ text: (text: NSAttributedString, size: CGSize, shortened: Bool), at point: CGPoint, from: Double, to: Double, trailing: Bool = false) -> CALayer {
    // Drawn as a picture: a text layer draws nothing when exported this way.
    let size = CGSize(width: (text.size.width + pad.width * 2).rounded(.up), height: (text.size.height + pad.height * 2).rounded(.up))
    let image = NSImage(size: size)
    image.lockFocus()
    NSColor(calibratedWhite: 0.08, alpha: 0.82).setFill()
    NSBezierPath(roundedRect: NSRect(origin: .zero, size: size), xRadius: fontSize * 0.5, yRadius: fontSize * 0.5).fill()
    text.text.draw(with: NSRect(x: pad.width, y: pad.height, width: text.size.width + 1, height: text.size.height), options: [.usesLineFragmentOrigin, .usesFontLeading])
    image.unlockFocus()
    let pill = CALayer()
    pill.contents = image.cgImage(forProposedRect: nil, context: nil, hints: nil)
    pill.contentsGravity = .resize
    pill.isGeometryFlipped = true
    pill.frame = CGRect(origin: CGPoint(x: trailing ? point.x - size.width : point.x, y: point.y), size: size)
    pill.opacity = 0
    let show = CABasicAnimation(keyPath: "opacity")
    show.fromValue = 1; show.toValue = 1
    show.beginTime = AVCoreAnimationBeginTimeAtZero + from
    show.duration = max(0.05, to - from)
    show.isRemovedOnCompletion = false
    show.fillMode = .removed
    pill.add(show, forKey: "shown")
    return pill
}
let videoLayer = CALayer(), parent = CALayer()
parent.frame = CGRect(origin: .zero, size: movieSize); videoLayer.frame = parent.frame
parent.isGeometryFlipped = true  // Top-left origin, as the picture has.
parent.addSublayer(videoLayer)
if band > 0 {
    // The source goes on below the crop, so the band is painted over it.
    let strip = CALayer()
    strip.frame = CGRect(x: 0, y: pictureSize.height, width: movieSize.width, height: band)
    strip.backgroundColor = CGColor(gray: 0, alpha: 1)
    parent.addSublayer(strip)
}
var shownMarks: [[String: Any]] = []
for (i, mark) in marks.enumerated() {
    let from = outTime(mark.0), to = i + 1 < marks.count ? outTime(marks[i + 1].0) : outTotal
    var shown: [String: Any] = ["seconds": tidy(from), "text": mark.1]
    if fittedMarks[i].shortened { shown["shown"] = fittedMarks[i].text.string }
    shownMarks.append(shown)
    guard to > from else { continue }
    parent.addSublayer(caption(fittedMarks[i], at: CGPoint(x: margin, y: pictureSize.height + margin), from: from, to: to))
}
var condensed: [[String: Any]] = []
for (i, segment) in segments.enumerated() where segment.fast {
    let skipped = segment.to - segment.from
    let text = skipped >= 60 ? String(format: "▶▶ %d min %02d s pass", Int(skipped) / 60, Int(skipped) % 60) : String(format: "▶▶ %d s pass", Int(skipped.rounded()))
    condensed.append(["from": tidy(segment.from), "to": tidy(segment.to), "seconds": tidy(skipped), "playsFor": tidy(segment.outDuration), "at": tidy(outStarts[i])])
    parent.addSublayer(caption(fitted(text, badge: true), at: CGPoint(x: pictureSize.width - margin, y: margin), from: outStarts[i], to: outStarts[i] + segment.outDuration, trailing: true))
}
video.animationTool = AVVideoCompositionCoreAnimationTool(postProcessingAsVideoLayer: videoLayer, in: parent)

guard let export = AVAssetExportSession(asset: edit, presetName: AVAssetExportPresetHighestQuality) else { fail("cannot export") }
export.outputURL = output
export.outputFileType = output.pathExtension.lowercased() == "mov" ? .mov : .mp4
export.videoComposition = video
export.exportAsynchronously { semaphore.signal() }
semaphore.wait()
guard export.status == .completed else { fail("export failed: \(export.error?.localizedDescription ?? "unknown error")") }
let report: [String: Any] = ["file": output.path, "inputSeconds": tidy(total), "seconds": tidy(outTotal),
                             "width": Int(movieSize.width), "height": Int(movieSize.height), "captionBand": Int(band), "crop": [Int(crop.minX), Int(crop.minY), Int(crop.width), Int(crop.height)],
                             "condensed": condensed, "marks": shownMarks]
print(String(data: try! JSONSerialization.data(withJSONObject: report, options: [.sortedKeys]), encoding: .utf8)!)
