import Foundation
import AppKit
import UserNotifications

enum Pref {
    static let outDir = "outDir", services = "services", resolver = "resolver",
               trackTimeout = "trackTimeout", noFallback = "noFallback",
               fallbackFlac = "fallbackFlac", fixCoversAfter = "fixCoversAfter",
               notify = "notify"
    static let defaultOut = NSHomeDirectory() + "/Music/FLAC"
    static let allServices = ["qobuz", "deezer", "tidal", "amazon"]

    static func register() {
        UserDefaults.standard.register(defaults: [
            outDir: defaultOut, services: allServices.joined(separator: ","), resolver: "auto",
            trackTimeout: 300, noFallback: false, fallbackFlac: false, fixCoversAfter: false,
            notify: true,
        ])
    }
    static var out: String {
        let s = UserDefaults.standard.string(forKey: outDir) ?? defaultOut
        return (s as NSString).expandingTildeInPath
    }
}

struct JobOptions: Equatable {
    var dryRun = false
    var redo = false
    var wholePlaylist = false
    var limit = 0
    var sendToIPod = false
    var ipodPlaylist = "@auto"      // "" none, "@auto" = YouTube playlist name, else a name
}

struct Job: Identifiable {
    enum State: String { case queued = "Queued", running = "Running", done = "Done",
                         partial = "Finished with failures", failed = "Failed", stopped = "Stopped" }
    let id = UUID()
    var urls: [String]
    var options: JobOptions
    var state: State = .queued
    var title: String
    var summary = ""
    var added = Date()
}

struct TrackRow: Identifiable {
    enum Status { case working, lossless, youtube, skipped, failed, matched, willUseYouTube
        var label: String {
            switch self {
            case .working: "Working…"
            case .lossless: "Lossless FLAC"
            case .youtube: "YouTube audio"
            case .skipped: "Already have it"
            case .failed: "Failed"
            case .matched: "Match found"
            case .willUseYouTube: "No lossless match"
            }
        }
    }
    let id: Int
    var title: String
    var status: Status = .working
    var detail = ""
    var file = ""
}

@MainActor
final class Downloader: ObservableObject {
    static let shared = Downloader()

    @Published var jobs: [Job] = []
    @Published var rows: [TrackRow] = []
    @Published var total = 0
    @Published var phase = ""
    @Published var currentJob: UUID?
    let runner = Runner()

    private var current = 0

    init() {
        runner.onLine = { [weak self] l in self?.parse(l) }
        runner.onExit = { [weak self] code, stopped in self?.jobEnded(code, stopped: stopped) }
    }

    var isBusy: Bool { runner.running }
    var doneCount: Int { rows.filter { $0.status != .working }.count }

    // MARK: queue

    func enqueue(text: String, options: JobOptions) -> Int {
        let urls = Self.extractURLs(text)
        guard !urls.isEmpty else { return 0 }
        let title = urls.count == 1 ? Self.shortTitle(urls[0]) : "\(urls.count) links"
        jobs.append(Job(urls: urls, options: options, title: title))
        startNextIfIdle()
        return urls.count
    }

    /// Re-run the manifest entries that ended in "error".
    func retryFailed(ids: [String]) {
        guard !ids.isEmpty else { return }
        let urls = ids.map { "https://www.youtube.com/watch?v=\($0)" }
        jobs.append(Job(urls: urls, options: JobOptions(), title: "Retry \(ids.count) failed track(s)"))
        startNextIfIdle()
    }

    func remove(_ job: Job) {
        guard job.state != .running else { return }
        jobs.removeAll { $0.id == job.id }
    }

    func clearFinished() { jobs.removeAll { $0.state != .queued && $0.state != .running } }

    func stop() {
        runner.stop()
    }

    func stopAll() {
        for i in jobs.indices where jobs[i].state == .queued { jobs[i].state = .stopped }
        runner.stop()
    }

    static func extractURLs(_ text: String) -> [String] {
        var out: [String] = []
        for token in text.split(whereSeparator: { $0 == " " || $0 == "\n" || $0 == "\t" || $0 == "," }) {
            let t = token.trimmingCharacters(in: CharacterSet(charactersIn: "<>\"'"))
            guard let u = URL(string: t), let host = u.host?.lowercased(),
                  host.contains("youtube.com") || host.contains("youtu.be") else { continue }
            if !out.contains(t) { out.append(t) }
        }
        return out
    }

    static func shortTitle(_ url: String) -> String {
        if url.contains("list=") && !url.contains("watch?") { return "Playlist" }
        if url.contains("list=") { return "Song (from a playlist)" }
        return "Song"
    }

    private func startNextIfIdle() {
        guard !runner.running, let i = jobs.firstIndex(where: { $0.state == .queued }) else { return }
        jobs[i].state = .running
        currentJob = jobs[i].id
        rows = []; total = 0; current = 0
        phase = "Reading the YouTube link…"
        runner.clear()

        let d = UserDefaults.standard
        let o = jobs[i].options
        var args = [Tools.ytflac, "-o", Pref.out]
        let services = (d.string(forKey: Pref.services) ?? "").split(separator: ",").map(String.init)
            .filter { Pref.allServices.contains($0) }
        args += ["--service"] + (services.isEmpty ? Pref.allServices : services)
        args += ["--resolver", d.string(forKey: Pref.resolver) ?? "auto"]
        args += ["--track-timeout", String(d.integer(forKey: Pref.trackTimeout))]
        if d.bool(forKey: Pref.noFallback) { args.append("--no-fallback") }
        if d.bool(forKey: Pref.fallbackFlac) { args.append("--fallback-flac") }
        if d.bool(forKey: Pref.fixCoversAfter) && !o.dryRun { args.append("--fix-covers") }
        if o.dryRun { args.append("--dry-run") }
        if o.redo { args.append("--redo") }
        if o.wholePlaylist { args.append("--yes-playlist") }
        if o.limit > 0 { args += ["--limit", String(o.limit)] }
        args += jobs[i].urls
        if !runner.start(Tools.python, args) {
            jobs[i].state = .failed
            jobs[i].summary = "Couldn't start Python (\(Tools.python)). Check the Setup page."
        }
    }

    private func jobEnded(_ code: Int32, stopped: Bool) {
        for r in rows.indices where rows[r].status == .working {
            rows[r].status = .failed
            rows[r].detail = stopped ? "Stopped" : "Didn't finish"
        }
        if let i = jobs.firstIndex(where: { $0.id == currentJob }) {
            if stopped { jobs[i].state = .stopped }
            else if code == 0 { jobs[i].state = .done }
            else if rows.contains(where: { $0.status == .lossless || $0.status == .youtube || $0.status == .skipped }) {
                jobs[i].state = .partial
            } else { jobs[i].state = .failed }
            if jobs[i].summary.isEmpty {
                jobs[i].summary = runner.lines.last(where: { $0.kind == .error })?.text ?? "Exit code \(code)"
            }
            phase = jobs[i].summary
            notify(jobs[i])
            if jobs[i].options.sendToIPod && !jobs[i].options.dryRun && !stopped { sendToIPod(jobs[i]) }
        }
        currentJob = nil
        Library.shared.rescanSoon()
        startNextIfIdle()
    }

    /// Everything this job produced — new files, plus songs it skipped because they were
    /// already downloaded (found through the manifest) — goes to the iPod.
    private func sendToIPod(_ job: Job) {
        let out = Pref.out
        var paths = rows.filter { ($0.status == .lossless || $0.status == .youtube) && !$0.file.isEmpty }
            .map { out + "/" + $0.file }
        let skipped = rows.filter { $0.status == .skipped }.map(\.title)
        if !skipped.isEmpty, let d = FileManager.default.contents(atPath: out + "/.ytflac-manifest.json"),
           let m = try? JSONSerialization.jsonObject(with: d) as? [String: Any] {
            for case let e as [String: Any] in m.values {
                guard let t = e["title"] as? String, skipped.contains(where: { t.hasPrefix($0) }) else { continue }
                for case let f as String in e["files"] as? [Any] ?? [] { paths.append(out + "/" + f) }
            }
        }
        paths = paths.filter { FileManager.default.fileExists(atPath: $0) }
        guard !paths.isEmpty else { return }
        let isPlaylist = !["Song", "Song (from a playlist)"].contains(job.title) && !job.title.hasSuffix(" links")
            && !job.title.hasPrefix("Retry ")
        let choice = job.options.ipodPlaylist
        let playlist = choice == "@auto" ? (isPlaylist ? job.title : nil) : (choice.isEmpty ? nil : choice)
        IPod.shared.send(paths: Array(Set(paths)).sorted(), playlist: playlist)
    }

    // MARK: output parsing (matches ytflac.py's info/good/warn messages)

    private static let indexRE = try! NSRegularExpression(pattern: #"\[(\d+)/(\d+)\]\s+(.*)$"#)
    private static let suffixes = [
        " — already downloaded, skipping", " — file was deleted; re-downloading",
        " — marked done but no file on disk; re-downloading",
        " — no lossless match; downloading the YouTube audio instead",
        " — no Spotify match; skipped (--no-fallback)",
    ]

    private func row(_ i: Int) -> Int? { rows.firstIndex { $0.id == i } }

    private func parse(_ line: String) {
        let ns = line as NSString
        // only ytflac's own lines carry the [i/N] counter (SpotiFLAC may print its own)
        let ours = line.hasPrefix("==> [") || line.hasPrefix(" ok  [") || line.hasPrefix("warn [")
            || line.hasPrefix("  [") || line.hasPrefix("warn unexpected error on [")
            || line.hasPrefix("warn YouTube fallback failed")
        if ours, let m = Self.indexRE.firstMatch(in: line, range: NSRange(location: 0, length: ns.length)) {
            let idx = Int(ns.substring(with: m.range(at: 1))) ?? 0
            let n = Int(ns.substring(with: m.range(at: 2))) ?? 0
            var title = ns.substring(with: m.range(at: 3))
            for s in Self.suffixes where title.hasSuffix(s) { title = String(title.dropLast(s.count)) }
            if n > 0 { total = n }
            if line.hasPrefix("warn unexpected error") || line.hasPrefix("warn YouTube fallback failed") {
                if let r = row(idx) { rows[r].status = .failed; rows[r].detail = "Unexpected error — will retry next run" }
                return
            }
            current = idx
            if row(idx) == nil { rows.append(TrackRow(id: idx, title: title)) }
            let r = row(idx)!
            if line.contains("already downloaded, skipping") { rows[r].status = .skipped; rows[r].detail = "In your library" }
            else if line.contains("no lossless match; downloading") { rows[r].detail = "No lossless source — getting YouTube audio" }
            else if line.contains("skipped (--no-fallback)") { rows[r].status = .failed; rows[r].detail = "No lossless source (purist mode)" }
            else if line.contains("re-downloading") { rows[r].detail = "File was missing — downloading again" }
            else { rows[r].detail = "Finding the song…" }
            phase = "Track \(idx) of \(n)"
            return
        }
        guard let r = row(current) else {
            if let m = line.range(of: #"Playlist '(.*)' — (\d+) tracks"#, options: .regularExpression) {
                let s = String(line[m])
                if let i = jobs.firstIndex(where: { $0.id == currentJob }) {
                    jobs[i].title = String(s.dropFirst(10).prefix(while: { $0 != "'" }))
                }
            } else if line.contains("Matching") && line.contains("track(s)") {
                phase = "Matching songs to Spotify…"
            }
            finishLine(line)
            return
        }
        var t = line.trimmingCharacters(in: .whitespaces)
        if t.hasPrefix("ok ") { t = "ok " + t.dropFirst(3).trimmingCharacters(in: .whitespaces) }
        if t.hasPrefix("-> no lossless match") {
            rows[r].status = .willUseYouTube; rows[r].detail = "Would download the YouTube audio"
        } else if t.hasPrefix("-> ") {
            var label = String(t.dropFirst(3))
            if let v = label.range(of: "  via ") { label = String(label[..<v.lowerBound]) }
            if let v = label.range(of: "  (") { label = String(label[..<v.lowerBound]); rows[r].status = .matched }
            rows[r].detail = "→ " + label
        } else if t.hasPrefix("ok saved (YouTube audio): ") {
            rows[r].status = .youtube; rows[r].file = String(t.dropFirst(26))
            rows[r].detail = "Saved as " + rows[r].file
        } else if t.hasPrefix("ok saved: ") {
            rows[r].status = .lossless; rows[r].file = String(t.dropFirst(10))
            rows[r].detail = "Saved as " + rows[r].file
        } else if t.hasPrefix("ok file already on disk") {
            rows[r].status = .lossless; rows[r].detail = "Already on disk"
        } else if line.hasPrefix("warn ") {
            let msg = String(line.dropFirst(5))
            if msg.contains("will retry next run") || msg.hasPrefix("YouTube fallback also failed") {
                rows[r].status = .failed; rows[r].detail = "Failed — will retry next run"
            } else if msg.hasPrefix("falling back to YouTube") {
                rows[r].detail = "Lossless services failed — getting YouTube audio"
            } else if msg.hasPrefix("no new FLAC") || msg.hasPrefix("SpotiFLAC failed") || msg.hasPrefix("track timed out")
                        || msg.hasPrefix("downloaded file failed") {
                rows[r].detail = msg
            }
        } else if rows[r].status == .working, rows[r].detail.hasPrefix("→ ") || rows[r].detail.hasPrefix("Lossless") {
            // SpotiFLAC's own progress output: show the latest line as live detail
            if t.count < 140, !t.hasPrefix("http") { rows[r].detail = rows[r].detail.components(separatedBy: "  ·  ")[0] + "  ·  " + t }
        }
        finishLine(line)
    }

    private func finishLine(_ line: String) {
        guard let i = jobs.firstIndex(where: { $0.id == currentJob }) else { return }
        if line.hasPrefix("==> Finished:") || line.hasPrefix("==> Dry run:") {
            jobs[i].summary = String(line.dropFirst(4))
            if let f = jobs[i].summary.range(of: " Files in:") { jobs[i].summary = String(jobs[i].summary[..<f.lowerBound]) }
            phase = jobs[i].summary
        } else if line.hasPrefix("error ") {
            jobs[i].summary = String(line.dropFirst(6))
        }
    }

    // MARK: notifications

    func requestNotifications() {
        guard Bundle.main.bundleIdentifier != nil else { return }
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { _, _ in }
    }

    private func notify(_ job: Job) {
        guard UserDefaults.standard.bool(forKey: Pref.notify), !NSApp.isActive, Bundle.main.bundleIdentifier != nil else { return }
        let c = UNMutableNotificationContent()
        c.title = "\(job.title): \(job.state.rawValue)"
        c.body = job.summary
        c.sound = .default
        UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: job.id.uuidString, content: c, trigger: nil))
    }
}
