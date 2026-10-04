import SwiftUI
import AppKit

enum Pane: String, CaseIterable, Identifiable {
    case download = "Download", library = "Library", ipod = "iPod", setup = "Setup"
    var id: String { rawValue }
    var icon: String {
        switch self {
        case .download: "arrow.down.circle"
        case .library: "music.note.list"
        case .ipod: "ipod"
        case .setup: "checklist"
        }
    }
}

struct ContentView: View {
    @AppStorage("lastPane") private var lastPane = Pane.download.rawValue
    @State private var section: Pane?
    @ObservedObject var dl = Downloader.shared
    @ObservedObject var lib = Library.shared
    @ObservedObject var pod = IPod.shared

    var body: some View {
        NavigationSplitView {
            List(Pane.allCases, selection: $section) { s in
                HStack {
                    Label(s.rawValue, systemImage: s.icon)
                    Spacer()
                    badge(s)
                }.tag(s)
            }
            .navigationSplitViewColumnWidth(min: 170, ideal: 190)
        } detail: {
            switch section ?? .download {
            case .download: DownloadView()
            case .library: LibraryView()
            case .ipod: IPodView()
            case .setup: SetupView()
            }
        }
        .onAppear { if section == nil { section = Pane(rawValue: lastPane) ?? .download } }
        .onChange(of: section) { _, v in if let v { lastPane = v.rawValue } }
        .onReceive(NotificationCenter.default.publisher(for: .showDownloads)) { _ in section = .download }
        .onReceive(NotificationCenter.default.publisher(for: .showIPod)) { _ in section = .ipod }
    }

    @ViewBuilder func badge(_ s: Pane) -> some View {
        switch s {
        case .download where dl.isBusy:
            Text(dl.total > 0 ? "\(dl.doneCount)/\(dl.total)" : "…").font(.caption).monospacedDigit().foregroundStyle(.secondary)
        case .library where !lib.songs.isEmpty:
            Text("\(lib.songs.count)").font(.caption).foregroundStyle(.secondary)
        case .ipod where pod.volume != nil:
            Image(systemName: pod.runner.running ? "arrow.triangle.2.circlepath" : "circle.fill")
                .font(.system(size: 7)).foregroundStyle(.green)
        default: EmptyView()
        }
    }
}

// MARK: - Download

struct DownloadView: View {
    @ObservedObject var dl = Downloader.shared
    @ObservedObject var runner = Downloader.shared.runner
    @State private var text = ""
    @State private var opts = JobOptions()
    @AppStorage("sendToIPod") private var sendToIPod = false
    @AppStorage("sendPlaylist") private var sendPlaylist = "@auto"
    @ObservedObject var pod = IPod.shared
    @State private var showLog = false
    @State private var note = ""

    var body: some View {
        VSplitView {
            VStack(alignment: .leading, spacing: 12) {
                input
                if !dl.jobs.isEmpty { queue }
            }
            .padding(16)
            .frame(minHeight: 200, idealHeight: 250)

            VStack(spacing: 0) {
                progressHeader
                Divider()
                if showLog {
                    LogView(runner: runner)
                } else if dl.rows.isEmpty {
                    placeholder
                } else {
                    trackList
                }
            }
            .frame(minHeight: 260)
        }
        .onAppear { pasteIfYouTube(onlyIfEmpty: true) }
    }

    var input: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Paste YouTube links").font(.headline)
            HStack(alignment: .top, spacing: 10) {
                TextEditor(text: $text)
                    .font(.system(.body, design: .monospaced))
                    .frame(height: 64)
                    .overlay(alignment: .topLeading) {
                        if text.isEmpty {
                            Text("A song or playlist link — or several, one per line")
                                .foregroundStyle(.tertiary).padding(.leading, 5).padding(.top, 1).allowsHitTesting(false)
                        }
                    }
                    .overlay(RoundedRectangle(cornerRadius: 6).stroke(.quaternary))
                VStack(spacing: 6) {
                    Button { go() } label: {
                        Label(opts.dryRun ? "Preview" : "Download", systemImage: opts.dryRun ? "eye" : "arrow.down.circle.fill")
                            .frame(width: 110)
                    }
                    .buttonStyle(.borderedProminent).controlSize(.large)
                    .keyboardShortcut(.return, modifiers: .command)
                    .disabled(Downloader.extractURLs(text).isEmpty)
                    Button { pasteIfYouTube(onlyIfEmpty: false) } label: { Label("Paste", systemImage: "doc.on.clipboard").frame(width: 110) }
                }
            }
            HStack(spacing: 14) {
                Toggle("Preview only", isOn: $opts.dryRun).help("Find the matches but don't download anything")
                Toggle("Whole playlist", isOn: $opts.wholePlaylist).help("When a song link is part of a playlist, download the whole playlist")
                Toggle("Download again", isOn: $opts.redo).help("Download songs again even if they're already in your library")
                Toggle(isOn: $sendToIPod) {
                    Label("Send to iPod when done", systemImage: "ipod")
                }.help(pod.volume == nil ? "The iPod isn't plugged in — songs will be copied as soon as it is"
                                          : "Copies the finished songs to the iPod")
                if sendToIPod {
                    PlaylistPicker(selection: $sendPlaylist, title: "into", allowAuto: true).frame(width: 290)
                }
                HStack(spacing: 4) {
                    Text("First")
                    TextField("all", value: $opts.limit, format: .number).frame(width: 44).textFieldStyle(.roundedBorder)
                    Text("songs")
                }.help("Only take the first N songs of a playlist (0 = all)")
            }
            .toggleStyle(.checkbox).font(.callout)
            if !note.isEmpty { Text(note).font(.callout).foregroundStyle(.secondary) }
        }
    }

    var queue: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text("Queue").font(.headline)
                Spacer()
                if dl.jobs.contains(where: { $0.state != .queued && $0.state != .running }) {
                    Button("Clear finished") { dl.clearFinished() }.buttonStyle(.link)
                }
            }
            ScrollView {
                VStack(spacing: 2) {
                    ForEach(dl.jobs) { j in
                        HStack(spacing: 8) {
                            jobIcon(j.state)
                            Text(j.title).fontWeight(j.state == .running ? .semibold : .regular).lineLimit(1)
                            if j.options.dryRun { Text("preview").font(.caption).foregroundStyle(.secondary) }
                            Text(j.summary.isEmpty ? j.state.rawValue : j.summary)
                                .font(.caption).foregroundStyle(.secondary).lineLimit(1)
                            Spacer()
                            if j.state == .running {
                                Button("Stop") { dl.stop() }.controlSize(.small)
                            } else {
                                Button { dl.remove(j) } label: { Image(systemName: "xmark.circle.fill") }
                                    .buttonStyle(.plain).foregroundStyle(.tertiary)
                            }
                        }
                        .padding(.vertical, 3).padding(.horizontal, 6)
                        .background(j.state == .running ? Color.accentColor.opacity(0.08) : .clear, in: RoundedRectangle(cornerRadius: 5))
                    }
                }
            }.frame(maxHeight: 110)
        }
    }

    @ViewBuilder func jobIcon(_ s: Job.State) -> some View {
        switch s {
        case .queued: Image(systemName: "clock").foregroundStyle(.secondary)
        case .running: ProgressView().controlSize(.small).frame(width: 16)
        case .done: Image(systemName: "checkmark.circle.fill").foregroundStyle(.green)
        case .partial: Image(systemName: "exclamationmark.circle.fill").foregroundStyle(.orange)
        case .failed: Image(systemName: "xmark.circle.fill").foregroundStyle(.red)
        case .stopped: Image(systemName: "stop.circle").foregroundStyle(.secondary)
        }
    }

    var progressHeader: some View {
        let counts = Dictionary(grouping: dl.rows, by: \.status).mapValues(\.count)
        return HStack(spacing: 14) {
            if dl.isBusy {
                if dl.total > 0 {
                    ProgressView(value: Double(dl.doneCount), total: Double(dl.total)).frame(width: 160)
                } else { ProgressView().controlSize(.small) }
            }
            Text(dl.phase.isEmpty ? "Nothing running" : dl.phase).lineLimit(1)
            Spacer()
            stat(counts[.lossless], "FLAC", .accentColor)
            stat(counts[.youtube], "YouTube audio", .orange)
            stat(counts[.skipped], "already had", .secondary)
            stat(counts[.failed], "failed", .red)
            stat(counts[.matched], "matched", .accentColor)
            stat(counts[.willUseYouTube], "no lossless", .orange)
            Picker("", selection: $showLog) {
                Image(systemName: "list.bullet").tag(false)
                Image(systemName: "terminal").tag(true)
            }.pickerStyle(.segmented).labelsHidden().frame(width: 80).help("Songs / full log")
        }
        .font(.callout)
        .padding(.horizontal, 14).padding(.vertical, 8)
    }

    @ViewBuilder func stat(_ n: Int?, _ label: String, _ c: Color) -> some View {
        if let n, n > 0 {
            HStack(spacing: 4) { Circle().fill(c).frame(width: 7, height: 7); Text("\(n) \(label)") }
        }
    }

    var placeholder: some View {
        VStack(spacing: 10) {
            Image(systemName: "waveform.badge.plus").font(.system(size: 44)).foregroundStyle(.secondary)
            Text("Paste a YouTube song or playlist link").font(.title3)
            Text("Each song is matched on Spotify and downloaded as real lossless FLAC from Qobuz, Deezer, Tidal or Amazon Music.\nIf no lossless copy exists, the YouTube audio is saved instead (.opus / .m4a).")
                .multilineTextAlignment(.center).foregroundStyle(.secondary).frame(maxWidth: 520)
        }.frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    var trackList: some View {
        ScrollViewReader { proxy in
            List(dl.rows) { r in
                HStack(spacing: 10) {
                    rowIcon(r.status).frame(width: 18)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(r.title).lineLimit(1)
                        Text(r.detail).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                    }
                    Spacer()
                    Text(r.status.label).font(.caption).foregroundStyle(.secondary)
                    if !r.file.isEmpty {
                        Button { Tools.reveal(Pref.out + "/" + r.file) } label: { Image(systemName: "magnifyingglass") }
                            .buttonStyle(.borderless).help("Show in Finder")
                    }
                }
                .padding(.vertical, 2)
                .id(r.id)
            }
            .onChange(of: dl.rows.count) { _, _ in
                if let last = dl.rows.last { withAnimation { proxy.scrollTo(last.id, anchor: .bottom) } }
            }
        }
    }

    @ViewBuilder func rowIcon(_ s: TrackRow.Status) -> some View {
        switch s {
        case .working: ProgressView().controlSize(.small)
        case .lossless: Image(systemName: "checkmark.seal.fill").foregroundStyle(Color.accentColor)
        case .youtube: Image(systemName: "checkmark.circle").foregroundStyle(.orange)
        case .skipped: Image(systemName: "checkmark").foregroundStyle(.secondary)
        case .failed: Image(systemName: "xmark.octagon.fill").foregroundStyle(.red)
        case .matched: Image(systemName: "link").foregroundStyle(Color.accentColor)
        case .willUseYouTube: Image(systemName: "questionmark.circle").foregroundStyle(.orange)
        }
    }

    private func go() {
        var o = opts
        o.sendToIPod = sendToIPod && !opts.dryRun
        o.ipodPlaylist = sendPlaylist
        let n = dl.enqueue(text: text, options: o)
        note = n == 0 ? "No YouTube links found in that text." :
            (dl.jobs.filter { $0.state == .queued }.isEmpty ? "" : "Added to the queue — it starts when the current job finishes.")
        if n > 0 { text = ""; opts.dryRun = false; opts.redo = false; opts.limit = 0 }
    }

    private func pasteIfYouTube(onlyIfEmpty: Bool) {
        guard !onlyIfEmpty || text.isEmpty, let s = NSPasteboard.general.string(forType: .string) else { return }
        let urls = Downloader.extractURLs(s)
        if !urls.isEmpty { text = urls.joined(separator: "\n") }
        else if !onlyIfEmpty { note = "The clipboard doesn't have a YouTube link." }
    }
}

// MARK: - shared log views

struct LogView: View {
    @ObservedObject var runner: Runner
    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 1) {
                    ForEach(runner.lines) { l in
                        Text(l.text)
                            .font(.system(size: 11.5, design: .monospaced))
                            .foregroundStyle(color(l.kind))
                            .textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .id(l.id)
                    }
                }.padding(8)
            }
            .background(Color(nsColor: .textBackgroundColor))
            .onChange(of: runner.lines.last?.id) { _, id in if let id { proxy.scrollTo(id, anchor: .bottom) } }
            .onAppear { if let id = runner.lines.last?.id { proxy.scrollTo(id, anchor: .bottom) } }
        }
    }
    func color(_ k: LogLine.Kind) -> Color {
        switch k {
        case .plain: .primary
        case .info: .accentColor
        case .good: .green
        case .warn: .orange
        case .error: .red
        }
    }
}

struct ToolSheet: View {
    @ObservedObject var runner: Runner
    let title: String
    @Environment(\.dismiss) private var dismiss
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text(title).font(.title3.bold())
                if runner.running { ProgressView().controlSize(.small) }
                Spacer()
                if runner.running { Button("Stop") { runner.stop() } }
                Button(runner.running ? "Hide" : "Done") { dismiss() }.keyboardShortcut(.defaultAction)
            }
            LogView(runner: runner).clipShape(RoundedRectangle(cornerRadius: 6))
        }
        .padding(16).frame(width: 760, height: 520)
    }
}

// MARK: - Setup

struct Check: Identifiable {
    let id: String
    let name: String
    var ok: Bool?
    var detail = ""
    let fix: String?
    let fixArgs: [String]?
}

@MainActor
final class SetupModel: ObservableObject {
    static let shared = SetupModel()
    @Published var checks: [Check] = []
    @Published var checking = false
    let runner = Runner()

    var allGood: Bool { !checks.isEmpty && checks.allSatisfy { $0.ok == true } }

    func run() {
        checking = true
        Task.detached {
            let py = await MainActor.run { Tools.python }
            let brew = Tools.which("brew")
            var list: [Check] = []
            let (pc, pv) = Tools.capture(py, ["--version"])
            list.append(Check(id: "py", name: "Python 3", ok: pc == 0, detail: pc == 0 ? "\(pv.trimmingCharacters(in: .whitespacesAndNewlines)) · \(py)" : "not found",
                              fix: brew.map { _ in "Install" }, fixArgs: brew.map { [$0, "install", "python"] }))
            for (id, name, pkg) in [("ytdlp", "yt-dlp", "yt-dlp"), ("ffmpeg", "ffmpeg", "ffmpeg")] {
                let p = Tools.which(id == "ytdlp" ? "yt-dlp" : "ffmpeg")
                var detail = "not installed"
                if let p {
                    let (_, v) = Tools.capture(p, id == "ytdlp" ? ["--version"] : ["-version"])
                    detail = (v.split(separator: "\n").first.map(String.init) ?? "") + " · " + p
                }
                list.append(Check(id: id, name: name, ok: p != nil, detail: detail,
                                  fix: brew.map { _ in p == nil ? "Install" : "Update" },
                                  fixArgs: brew.map { [$0, p == nil ? "install" : "upgrade", pkg] }))
            }
            let probe = "import importlib.util as u,sys;print(u.find_spec('SpotiFLAC') is not None, u.find_spec('mutagen') is not None)"
            let (_, mods) = Tools.capture(py, ["-c", probe])
            let flags = mods.trimmingCharacters(in: .whitespacesAndNewlines).split(separator: " ")
            let hasSF = flags.first == "True", hasMut = flags.count > 1 && flags[1] == "True"
            let pip = [py, "-m", "pip", "install", "--break-system-packages", "--upgrade"]
            list.append(Check(id: "spotiflac", name: "SpotiFLAC engine", ok: hasSF,
                              detail: hasSF ? "installed (lossless downloads)" : "missing — needed for lossless downloads",
                              fix: hasSF ? "Update" : "Install",
                              fixArgs: pip + ["git+https://github.com/ShuShuzinhuu/SpotiFLAC-Module-Version", "nodriver"]))
            list.append(Check(id: "mutagen", name: "mutagen (cover art)", ok: hasMut,
                              detail: hasMut ? "installed" : "missing — cover art falls back to ffmpeg",
                              fix: hasMut ? nil : "Install", fixArgs: hasMut ? nil : pip + ["mutagen"]))
            let out = await MainActor.run { Pref.out }
            let exists = FileManager.default.fileExists(atPath: out)
            list.append(Check(id: "out", name: "Music folder", ok: exists,
                              detail: (out as NSString).abbreviatingWithTildeInPath, fix: nil, fixArgs: nil))
            let final = list
            await MainActor.run { self.checks = final; self.checking = false }
        }
    }

    func fix(_ c: Check) {
        guard let a = c.fixArgs, !runner.running else { return }
        runner.clear()
        runner.onExit = { [weak self] _, _ in self?.run() }
        runner.start(a[0], Array(a.dropFirst()))
    }
}

struct SetupView: View {
    @ObservedObject var m = SetupModel.shared
    @ObservedObject var runner = SetupModel.shared.runner
    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Setup").font(.largeTitle.bold())
                    Text("Programs Ritom Music uses behind the scenes. Everything should be green.").foregroundStyle(.secondary)
                }
                Spacer()
                Button { m.run() } label: { Label("Check again", systemImage: "arrow.clockwise") }.disabled(m.checking)
            }
            VStack(spacing: 0) {
                ForEach(m.checks) { c in
                    HStack(spacing: 12) {
                        Image(systemName: c.ok == true ? "checkmark.circle.fill" : "exclamationmark.triangle.fill")
                            .foregroundStyle(c.ok == true ? .green : .orange).font(.title3)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(c.name).fontWeight(.medium)
                            Text(c.detail).font(.caption).foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle)
                        }
                        Spacer()
                        if let f = c.fix { Button(f) { m.fix(c) }.disabled(runner.running) }
                    }
                    .padding(.vertical, 9).padding(.horizontal, 12)
                    Divider()
                }
            }
            .background(.background.secondary, in: RoundedRectangle(cornerRadius: 10))
            if m.checking { ProgressView().controlSize(.small) }
            Text("Tip: if downloads suddenly start failing, update yt-dlp and the SpotiFLAC engine first — YouTube and the music services change often.")
                .font(.callout).foregroundStyle(.secondary)
            if runner.running || !runner.lines.isEmpty {
                LogView(runner: runner).clipShape(RoundedRectangle(cornerRadius: 8)).frame(minHeight: 160)
            } else { Spacer() }
        }
        .padding(20)
        .onAppear { if m.checks.isEmpty { m.run() } }
    }
}

// MARK: - Settings (⌘,)

struct SettingsView: View {
    @AppStorage(Pref.outDir) private var outDir = Pref.defaultOut
    @AppStorage(Pref.services) private var services = Pref.allServices.joined(separator: ",")
    @AppStorage(Pref.resolver) private var resolver = "auto"
    @AppStorage(Pref.trackTimeout) private var trackTimeout = 300
    @AppStorage(Pref.noFallback) private var noFallback = false
    @AppStorage(Pref.fallbackFlac) private var fallbackFlac = false
    @AppStorage(Pref.fixCoversAfter) private var fixCoversAfter = false
    @AppStorage(Pref.notify) private var notify = true

    var order: [String] { services.split(separator: ",").map(String.init).filter { Pref.allServices.contains($0) } }

    var body: some View {
        Form {
            Section("Where music goes") {
                HStack {
                    Text(outDir.replacingOccurrences(of: NSHomeDirectory(), with: "~")).lineLimit(1).truncationMode(.middle)
                    Spacer()
                    Button("Change…") { pickFolder() }
                    Button("Show") { NSWorkspace.shared.open(URL(fileURLWithPath: Pref.out)) }
                }
                Text("Songs already downloaded to this folder are remembered and skipped next time.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section("Lossless services (tried top to bottom)") {
                ForEach(Array(order.enumerated()), id: \.element) { i, s in
                    HStack {
                        Text(s.capitalized)
                        Spacer()
                        Button { move(i, -1) } label: { Image(systemName: "chevron.up") }.buttonStyle(.borderless).disabled(i == 0)
                        Button { move(i, 1) } label: { Image(systemName: "chevron.down") }.buttonStyle(.borderless).disabled(i == order.count - 1)
                        Button { toggle(s) } label: { Image(systemName: "minus.circle") }.buttonStyle(.borderless).disabled(order.count == 1)
                    }
                }
                let off = Pref.allServices.filter { !order.contains($0) }
                if !off.isEmpty {
                    HStack {
                        Text("Turned off:").foregroundStyle(.secondary)
                        ForEach(off, id: \.self) { s in Button("+ \(s.capitalized)") { toggle(s) } }
                    }
                }
            }
            Section("Matching and fallbacks") {
                Picker("Find songs using", selection: $resolver) {
                    Text("Spotify search, then song.link (recommended)").tag("auto")
                    Text("Spotify search only (fastest)").tag("spotify")
                    Text("song.link only (exact, slow, rate-limited)").tag("odesli")
                }
                Toggle("Skip songs with no lossless copy (don't save YouTube audio)", isOn: $noFallback)
                Toggle("Save YouTube-audio fallbacks as .flac (still lossy inside)", isOn: $fallbackFlac).disabled(noFallback)
                Toggle("After each download, add missing cover art to the whole folder", isOn: $fixCoversAfter)
                Stepper("Give up on one song after \(trackTimeout == 0 ? "never" : "\(trackTimeout / 60) min")",
                        value: $trackTimeout, in: 0...1800, step: 60)
            }
            Section("App") {
                Toggle("Notify me when a download finishes in the background", isOn: $notify)
            }
        }
        .formStyle(.grouped)
        .frame(width: 540, height: 600)
    }

    private func move(_ i: Int, _ d: Int) {
        var o = order; o.swapAt(i, i + d); services = o.joined(separator: ",")
    }
    private func toggle(_ s: String) {
        var o = order
        if let i = o.firstIndex(of: s) { o.remove(at: i) } else { o.append(s) }
        services = o.joined(separator: ",")
    }
    private func pickFolder() {
        let p = NSOpenPanel()
        p.canChooseDirectories = true; p.canChooseFiles = false; p.canCreateDirectories = true
        p.directoryURL = URL(fileURLWithPath: Pref.out)
        if p.runModal() == .OK, let u = p.url { outDir = u.path; Library.shared.scan() }
    }
}
