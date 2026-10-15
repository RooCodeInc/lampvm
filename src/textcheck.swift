// Runs on the host after `record --text-check`: reads the frames of a movie and reports any that show the given words.
// Usage: textcheck MOVIE WORD...   Exit 0: none shown, 1: some shown, 2: the check itself could not be trusted.
//
// A check that reads nothing must not pass, so each word is first drawn alone into a control picture and has to be read
// back. Every frame of the movie is read, so a single frame's flash is caught too; the recorder writes a frame only when the
// screen changes, so a mostly still take has few.
import AppKit
import AVFoundation
import Vision

func fail(_ message: String) -> Never { FileHandle.standardError.write(Data((message + "\n").utf8)); exit(2) }
let args = Array(CommandLine.arguments.dropFirst())
guard args.count >= 2 else { fail("usage: textcheck MOVIE WORD...") }
let shown = args.dropFirst().map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
let words = shown.map { $0.lowercased() }
guard !words.isEmpty else { fail("no words to look for") }

func lines(_ handler: VNImageRequestHandler) -> [String] {
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = false  // Read what is shown, not a corrected guess at it.
    do { try handler.perform([request]) } catch { fail("text recognition failed: \(error.localizedDescription)") }
    return (request.results ?? []).compactMap { $0.topCandidates(1).first?.string }
}
func found(_ text: [String]) -> [(String, String)] {
    text.flatMap { line in words.filter { line.lowercased().contains($0) }.map { ($0, line) } }
}

// One picture per word: read together, one word's line could change how Vision reads another's.
func control(_ word: String) -> CGImage? {
    let size = NSSize(width: 1400, height: 140), image = NSImage(size: size)
    image.lockFocus()
    NSColor.white.setFill(); NSRect(origin: .zero, size: size).fill()
    (word as NSString).draw(at: NSPoint(x: 40, y: 50), withAttributes: [.font: NSFont.systemFont(ofSize: 40), .foregroundColor: NSColor.black])
    image.unlockFocus()
    return image.cgImage(forProposedRect: nil, context: nil, hints: nil)
}
let missed = zip(shown, words).filter { word, lowered in
    guard let picture = control(word) else { fail("cannot draw the control picture") }
    return !lines(VNImageRequestHandler(cgImage: picture)).contains { $0.lowercased().contains(lowered) }
}.map { $0.0 }
guard missed.isEmpty else { fail("the check cannot read these words even in a plain picture, so it cannot vouch for them: \(missed.joined(separator: ", "))") }

let asset = AVURLAsset(url: URL(fileURLWithPath: args[0]))
let semaphore = DispatchSemaphore(value: 0)
var loaded: [AVAssetTrack] = []
Task { loaded = (try? await asset.loadTracks(withMediaType: .video)) ?? []; semaphore.signal() }
semaphore.wait()
guard let track = loaded.first, let reader = try? AVAssetReader(asset: asset) else { fail("cannot read the movie: \(args[0])") }
let output = AVAssetReaderTrackOutput(track: track, outputSettings: [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA])
reader.add(output)
guard reader.startReading() else { fail("cannot read the movie: \(reader.error?.localizedDescription ?? "unknown error")") }

let group = DispatchGroup(), lock = NSLock(), slots = DispatchSemaphore(value: ProcessInfo.processInfo.activeProcessorCount)
var hits: [[String: Any]] = [], read = 0, total = 0
func check(_ sample: CMSampleBuffer, at seconds: Double) {
    guard let pixels = CMSampleBufferGetImageBuffer(sample) else { return }
    slots.wait(); group.enter()
    DispatchQueue.global().async {
        let text = lines(VNImageRequestHandler(cvPixelBuffer: pixels))
        lock.lock()
        read += 1
        for (word, line) in found(text) { hits.append(["seconds": (seconds * 100).rounded() / 100, "word": word, "text": line]) }
        lock.unlock(); slots.signal(); group.leave()
    }
}
while let sample = output.copyNextSampleBuffer() {
    total += 1
    check(sample, at: CMTimeGetSeconds(sample.presentationTimeStamp))
}
group.wait()
guard reader.status == .completed else { fail("the movie could not be read to the end: \(reader.error?.localizedDescription ?? "unknown error")") }
guard read > 0 else { fail("no frame could be read from the movie") }
// One entry per word shown: how many frames, from when to when, and one line of text it was read in.
let matches: [[String: Any]] = words.compactMap { word in
    let shown = hits.filter { $0["word"] as! String == word }.sorted { ($0["seconds"] as! Double) < ($1["seconds"] as! Double) }
    guard let first = shown.first, let last = shown.last else { return nil }
    return ["word": word, "frames": Set(shown.map { $0["seconds"] as! Double }).count, "firstSeconds": first["seconds"]!, "lastSeconds": last["seconds"]!, "text": first["text"]!]
}
let report: [String: Any] = ["words": words, "framesInMovie": total, "framesRead": read, "control": "read", "matches": matches]
print(String(data: try! JSONSerialization.data(withJSONObject: report, options: [.sortedKeys]), encoding: .utf8)!)
exit(hits.isEmpty ? 0 : 1)
