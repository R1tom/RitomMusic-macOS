import Foundation
import AppKit

// Paths to the bundled Python tools and the command-line programs they need.
enum Tools {
    static let extraPath = "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

    static var env: [String: String] {
        var e = ProcessInfo.processInfo.environment
        e["PATH"] = extraPath + ":" + (e["PATH"] ?? "")
        e["PYTHONUNBUFFERED"] = "1"
        e["PYTHONIOENCODING"] = "utf-8"
        e["LANG"] = e["LANG"] ?? "en_US.UTF-8"
        return e
    }

    static func which(_ name: String) -> String? {
        for dir in extraPath.split(separator: ":") {
            let p = "\(dir)/\(name)"
            if FileManager.default.isExecutableFile(atPath: p) { return p }
        }
        return nil
    }

    /// python3 that has the SpotiFLAC module (Homebrew's), else any python3.
    static var python: String {
        if let o = UserDefaults.standard.string(forKey: "pythonPath"), !o.isEmpty,
           FileManager.default.isExecutableFile(atPath: o) { return o }
        return which("python3") ?? "/usr/bin/python3"
    }

    static var engineDir: String { (Bundle.main.resourcePath ?? ".") + "/engine" }
    static var ytflac: String { engineDir + "/ytflac.py" }
    static var verify: String { engineDir + "/verify_downloads.py" }
    static var ipodSync: String { engineDir + "/ipod-tool/ipod_sync.py" }

    /// Run a short command synchronously (call off the main thread).
    static func capture(_ exe: String, _ args: [String], timeout: TimeInterval = 60) -> (Int32, String) {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: exe)
        p.arguments = args
        p.environment = env
        let pipe = Pipe()
        p.standardOutput = pipe; p.standardError = pipe
        p.standardInput = FileHandle.nullDevice
        do { try p.run() } catch { return (-1, error.localizedDescription) }
        let deadline = Date().addingTimeInterval(timeout)
        DispatchQueue.global().async {
            while p.isRunning && Date() < deadline { usleep(100_000) }
            if p.isRunning { p.terminate() }
        }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        return (p.terminationStatus, String(decoding: data, as: UTF8.self))
    }

    /// Every descendant pid of `root` (ytflac starts SpotiFLAC / yt-dlp in their own sessions,
    /// so stopping only the Python process would leave them running).
    static func descendants(of root: pid_t) -> [pid_t] {
        let (_, out) = capture("/bin/ps", ["-Ao", "pid=,ppid="], timeout: 10)
        var children: [pid_t: [pid_t]] = [:]
        for line in out.split(separator: "\n") {
            let f = line.split(separator: " ", omittingEmptySubsequences: true)
            if f.count == 2, let pid = pid_t(f[0]), let ppid = pid_t(f[1]) { children[ppid, default: []].append(pid) }
        }
        var result: [pid_t] = [], queue = [root]
        while let p = queue.popLast() {
            for c in children[p] ?? [] { result.append(c); queue.append(c) }
        }
        return result
    }

    static func reveal(_ path: String) {
        NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: path)])
    }
}

struct LogLine: Identifiable {
    let id: Int
    let text: String
    var kind: Kind {
        if text.hasPrefix("warn ") { return .warn }
        if text.hasPrefix("error ") || text.hasPrefix("ERROR") || text.contains("Traceback") { return .error }
        if text.hasPrefix(" ok ") || text.hasPrefix("DONE") { return .good }
        if text.hasPrefix("==> ") || text.hasPrefix("$ ") { return .info }
        return .plain
    }
    enum Kind { case plain, info, good, warn, error }
}

/// Runs one child process at a time and streams its merged stdout/stderr line by line.
@MainActor
final class Runner: ObservableObject {
    @Published private(set) var lines: [LogLine] = []
    @Published private(set) var running = false
    @Published private(set) var lastExit: Int32?

    var onLine: ((String) -> Void)?
    var onExit: ((Int32, Bool) -> Void)?   // (exit code, stopped by user)

    private var proc: Process?
    private var pending = Data()
    private var counter = 0
    private var gotEOF = false, terminated = false, exitCode: Int32 = 0
    private var stoppedByUser = false
    private var activity: NSObjectProtocol?
    private let maxLines = 6000

    func clear() { lines.removeAll() }

    func append(_ s: String) {
        counter += 1
        lines.append(LogLine(id: counter, text: s))
        if lines.count > maxLines { lines.removeFirst(lines.count - maxLines) }
    }

    @discardableResult
    func start(_ exe: String, _ args: [String], label: String? = nil, keepAwake: Bool = true) -> Bool {
        guard !running else { return false }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: exe)
        p.arguments = args
        p.environment = Tools.env
        p.currentDirectoryURL = URL(fileURLWithPath: NSHomeDirectory())
        let pipe = Pipe()
        p.standardOutput = pipe; p.standardError = pipe
        p.standardInput = FileHandle.nullDevice
        gotEOF = false; terminated = false; stoppedByUser = false; pending.removeAll()

        pipe.fileHandleForReading.readabilityHandler = { [weak self] h in
            let d = h.availableData
            DispatchQueue.main.async { MainActor.assumeIsolated { self?.consume(d) } }
        }
        p.terminationHandler = { [weak self] pr in
            let code = pr.terminationStatus
            DispatchQueue.main.async { MainActor.assumeIsolated { self?.terminated(code) } }
        }
        append("$ " + (label ?? ([URL(fileURLWithPath: exe).lastPathComponent] + args.map(shortArg)).joined(separator: " ")))
        do { try p.run() } catch {
            append("error could not start \(exe): \(error.localizedDescription)")
            pipe.fileHandleForReading.readabilityHandler = nil
            lastExit = -1
            onExit?(-1, false)
            return false
        }
        proc = p
        running = true
        lastExit = nil
        if keepAwake {
            activity = ProcessInfo.processInfo.beginActivity(
                options: [.idleSystemSleepDisabled, .userInitiated], reason: "Downloading / converting music")
        }
        return true
    }

    private func shortArg(_ a: String) -> String {
        let a = a.replacingOccurrences(of: Tools.engineDir + "/", with: "")
        return a.contains(" ") ? "\"\(a)\"" : a
    }

    func stop() {
        guard let p = proc, p.isRunning else { return }
        stoppedByUser = true
        let pid = p.processIdentifier
        let kids = Tools.descendants(of: pid)
        kill(pid, SIGINT)
        for k in kids { kill(k, SIGTERM) }
        DispatchQueue.main.asyncAfter(deadline: .now() + 4) {
            if p.isRunning { kill(pid, SIGKILL) }
            for k in kids { kill(k, SIGKILL) }
        }
        append("warn stopped by you")
    }

    private func consume(_ d: Data) {
        if d.isEmpty {
            gotEOF = true
            proc.map { ($0.standardOutput as? Pipe)?.fileHandleForReading.readabilityHandler = nil }
            flush(final: true)
            finishIfDone()
            return
        }
        pending.append(d)
        flush(final: false)
    }

    private func flush(final: Bool) {
        // split on \n and \r (progress bars redraw with \r)
        while let i = pending.firstIndex(where: { $0 == 10 || $0 == 13 }) {
            let chunk = pending[pending.startIndex..<i]
            pending.removeSubrange(pending.startIndex...i)
            emit(String(decoding: chunk, as: UTF8.self))
        }
        if final, !pending.isEmpty {
            emit(String(decoding: pending, as: UTF8.self)); pending.removeAll()
        }
    }

    private func emit(_ raw: String) {
        // drop ANSI colour codes
        let s = raw.replacingOccurrences(of: "\u{1B}\\[[0-9;?]*[A-Za-z]", with: "", options: .regularExpression)
        guard !s.trimmingCharacters(in: .whitespaces).isEmpty else { return }
        append(s)
        onLine?(s)
    }

    private func terminated(_ code: Int32) {
        terminated = true; exitCode = code
        // EOF may never arrive if a grandchild still holds the pipe; don't wait forever
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { [weak self] in
            guard let self, self.running, !self.gotEOF else { return }
            self.gotEOF = true
            self.flush(final: true)
            self.finishIfDone()
        }
        finishIfDone()
    }

    private func finishIfDone() {
        guard gotEOF, terminated, running else { return }
        if let p = proc { (p.standardOutput as? Pipe)?.fileHandleForReading.readabilityHandler = nil }
        running = false
        proc = nil
        if let a = activity { ProcessInfo.processInfo.endActivity(a); activity = nil }
        lastExit = exitCode
        onExit?(exitCode, stoppedByUser)
    }
}
