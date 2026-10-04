import Foundation
import AppKit
import SwiftUI
import UniformTypeIdentifiers
import UserNotifications

struct PodTrack: Identifiable, Hashable {
    let id: String          // dbid (64-bit, kept as text)
    let title, artist, album, kind: String
    let size: Int64
    let seconds: Int
    var plays = 0, skips = 0
    var lastPlayed: Date?
    var lastPlayedValue: Double { lastPlayed?.timeIntervalSince1970 ?? 0 }   // sortable
}

/// Play history for one song, kept on the Mac. The iPod's "Play Counts" file only counts plays since it last
/// loaded the database and may start again from zero after a database rewrite, so totals are carried over here.
struct PlayStat: Codable {
    var base = 0, seen = 0, skipBase = 0, skipSeen = 0
    var last: Double = 0
}

enum PlayCounts {
    static let mostPlayed = "\u{1}most-played"     // sidebar id, can't clash with a real playlist name

    static var historyURL: URL {
        let dir = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("Ritom Music")
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir.appendingPathComponent("playcounts.json")
    }

    /// <iPod>/iPod_Control/iTunes/Play Counts: "mhdp" header, then one entry per track in database order:
    /// plays, last played (Mac epoch, local time), bookmark, rating, ?, skips, last skipped — all UInt32 LE.
    static func read(_ path: String) -> [(plays: Int, skips: Int, last: Double)] {
        guard let d = FileManager.default.contents(atPath: path), d.count >= 16,
              String(data: d.prefix(4), encoding: .ascii) == "mhdp" else { return [] }
        func u32(_ o: Int) -> Int {
            guard o + 4 <= d.count else { return 0 }
            return d[d.startIndex + o ..< d.startIndex + o + 4].enumerated().reduce(0) { $0 | Int($1.element) << (8 * $1.offset) }
        }
        let header = u32(4), size = u32(8), count = u32(12)
        guard size >= 4, header + size * count <= d.count else { return [] }
        let tz = Double(TimeZone.current.secondsFromGMT())
        return (0..<count).map { i in
            let o = header + i * size
            let mac = u32(o + 4)
            return (u32(o), size >= 24 ? u32(o + 20) : 0, mac > 0 ? Double(mac) - 2_082_844_800 - tz : 0)
        }
    }

    static func load() -> [String: PlayStat] {
        guard let d = try? Data(contentsOf: historyURL) else { return [:] }
        return (try? JSONDecoder().decode([String: PlayStat].self, from: d)) ?? [:]
    }

    static func save(_ h: [String: PlayStat]) {
        if let d = try? JSONEncoder().encode(h) { try? d.write(to: historyURL, options: .atomic) }
    }
}

struct PodPlaylist: Identifiable, Hashable {
    var id: String { name }
    let name: String
    let dbids: [String]
}

@MainActor
final class IPod: ObservableObject {
    static let shared = IPod()

    @Published var volume: String?
    @Published var tracks: [PodTrack] = []
    @Published var playlists: [PodPlaylist] = []
    @Published var freeBytes: Int64 = 0
    @Published var totalBytes: Int64 = 0
    @Published var deviceName = "iPod"
    @Published var needsImport = false
    @Published var lastMessage = ""
    @Published var ejectState: EjectState = .idle
    @Published var pending: [(paths: [String], playlist: String?)] = []   // waiting for the iPod
    let runner = Runner()

    /// Playlist names on the iPod — live when plugged in, remembered from last time otherwise.
    var knownPlaylists: [String] {
        volume != nil && !needsImport ? playlists.map(\.name)
            : (UserDefaults.standard.stringArray(forKey: "ipodPlaylistNames") ?? [])
    }

    var pendingCount: Int { pending.reduce(0) { $0 + $1.paths.count } }

    /// Add now if the iPod is here and idle, otherwise hold until it is.
    func send(paths: [String], playlist: String?) {
        pending.append((paths, playlist))
        flushPending()
    }

    func flushPending() {
        guard volume != nil, !needsImport, !runner.running, !pending.isEmpty else { return }
        let next = pending.removeFirst()
        add(paths: next.paths, playlist: next.playlist, switchTo: false)
    }

    init() {
        let nc = NSWorkspace.shared.notificationCenter
        for n in [NSWorkspace.didMountNotification, NSWorkspace.didUnmountNotification] {
            nc.addObserver(forName: n, object: nil, queue: .main) { _ in
                MainActor.assumeIsolated { IPod.shared.detect() }
            }
        }
        runner.onExit = { [weak self] code, _ in
            guard let self else { return }
            self.lastMessage = self.runner.lines.last(where: { !$0.text.hasPrefix("$ ") })?.text ?? "exit \(code)"
            self.reload()
            if self.ejectState == .waiting { self.doEject() } else { self.flushPending() }
        }
        detect()
    }

    func detect() {
        let fm = FileManager.default
        var found: String?
        if let env = ProcessInfo.processInfo.environment["IPOD_VOL"], fm.fileExists(atPath: env + "/iPod_Control/Music") {
            found = env
        } else if let vols = try? fm.contentsOfDirectory(atPath: "/Volumes") {
            found = vols.sorted().map { "/Volumes/" + $0 }.first { fm.fileExists(atPath: $0 + "/iPod_Control/Music") }
        }
        if found != volume {
            volume = found
            if found != nil, case .ejected = ejectState { ejectState = .idle }
        }
        reload()
        flushPending()
    }

    func reload() {
        guard let vol = volume, FileManager.default.fileExists(atPath: vol + "/iPod_Control") else {
            volume = nil; tracks = []; playlists = []; return
        }
        if let a = try? FileManager.default.attributesOfFileSystem(forPath: vol) {
            freeBytes = (a[.systemFreeSize] as? NSNumber)?.int64Value ?? 0
            totalBytes = (a[.systemSize] as? NSNumber)?.int64Value ?? 0
        }
        let catalog = vol + "/.ipod_sync/catalog.json"
        guard let d = FileManager.default.contents(atPath: catalog),
              let obj = try? JSONSerialization.jsonObject(with: d) else {
            // never managed by ipod-tool yet: its first command imports the existing library
            needsImport = true; tracks = []; playlists = []; return
        }
        needsImport = false
        var rawTracks: [[String: Any]] = [], rawPls: [[String: Any]] = []
        if let list = obj as? [[String: Any]] { rawTracks = list }
        else if let dict = obj as? [String: Any] {
            rawTracks = dict["tracks"] as? [[String: Any]] ?? []
            rawPls = dict["playlists"] as? [[String: Any]] ?? []
            if let meta = dict["meta"] as? [String: Any], let n = meta["device_name"] as? String { deviceName = n }
        }
        func idString(_ v: Any?) -> String { v.map { "\($0)" } ?? "" }
        tracks = rawTracks.map { t in
            PodTrack(id: idString(t["dbid"]), title: t["title"] as? String ?? "", artist: t["artist"] as? String ?? "",
                     album: t["album"] as? String ?? "",
                     kind: (t["filetype"] as? String ?? "").replacingOccurrences(of: " audio file", with: ""),
                     size: (t["size"] as? NSNumber)?.int64Value ?? 0,
                     seconds: ((t["tracklen"] as? NSNumber)?.intValue ?? 0) / 1000)
        }
        applyPlayCounts(vol)
        playlists = rawPls.map { p in
            PodPlaylist(name: p["name"] as? String ?? "?", dbids: (p["dbids"] as? [Any] ?? []).map { idString($0) })
        }
        UserDefaults.standard.set(playlists.map(\.name), forKey: "ipodPlaylistNames")
    }

    /// Merges the iPod's play counts into the history on the Mac and puts the totals on the tracks.
    private func applyPlayCounts(_ vol: String) {
        let raw = PlayCounts.read(vol + "/iPod_Control/iTunes/Play Counts")
        var hist = PlayCounts.load()
        var changed = false
        // entries follow the order of the database the iPod last loaded; only trust them while that matches ours
        let aligned = raw.count == tracks.count
        for i in tracks.indices {
            var s = hist[tracks[i].id] ?? PlayStat()
            if aligned {
                let e = raw[i]
                var n = s
                if e.plays < n.seen { n.base += n.seen }                  // iPod started counting again
                if e.skips < n.skipSeen { n.skipBase += n.skipSeen }
                n.seen = e.plays; n.skipSeen = e.skips; n.last = max(n.last, e.last)
                if n.seen != s.seen || n.base != s.base || n.skipSeen != s.skipSeen || n.skipBase != s.skipBase || n.last != s.last {
                    s = n; hist[tracks[i].id] = n; changed = true
                }
            }
            tracks[i].plays = s.base + s.seen
            tracks[i].skips = s.skipBase + s.skipSeen
            tracks[i].lastPlayed = s.last > 0 ? Date(timeIntervalSince1970: s.last) : nil
        }
        if changed { PlayCounts.save(hist) }
    }

    // MARK: commands (all go through ipod_sync.py)

    private func run(_ label: String, _ args: [String]) {
        guard let vol = volume, !runner.running else { return }
        runner.clear()
        runner.start(Tools.python, [Tools.ipodSync, "--volume", vol] + args, label: label)
    }

    func add(paths: [String], playlist: String? = nil, switchTo: Bool = true) {
        guard !paths.isEmpty else { return }
        let d = UserDefaults.standard
        var args = ["add"] + paths + ["--format", d.string(forKey: "ipodFormat") ?? "aac",
                                     "--bitrate", String(d.integer(forKey: "ipodBitrate") == 0 ? 256 : d.integer(forKey: "ipodBitrate"))]
        if let p = playlist?.trimmingCharacters(in: .whitespaces), !p.isEmpty { args += ["--playlist", p] }
        run("Adding \(paths.count == 1 ? URL(fileURLWithPath: paths[0]).lastPathComponent : "\(paths.count) items") to the iPod", args)
        if switchTo { NotificationCenter.default.post(name: .showIPod, object: nil) }
    }
    func remove(_ ids: Set<String>) {
        run("Removing \(ids.count) track(s)", ["remove", "--dbids", ids.joined(separator: ","), "--yes"])
    }
    func addToPlaylist(_ name: String, _ ids: Set<String>) {
        run("Adding \(ids.count) track(s) to “\(name)”", ["playlist", "add", name, "--dbids", ids.joined(separator: ",")])
    }
    func removeFromPlaylist(_ name: String, _ ids: Set<String>) {
        run("Removing \(ids.count) track(s) from “\(name)”", ["playlist", "remove", name, "--dbids", ids.joined(separator: ",")])
    }
    func deletePlaylist(_ name: String) { run("Deleting playlist “\(name)”", ["playlist", "delete", name, "--yes"]) }
    func renamePlaylist(_ old: String, to new: String) { run("Renaming “\(old)”", ["playlist", "rename", old, "--search", new]) }
    func rebuild() { run("Rebuilding the iPod database", ["rebuild"]) }
    func importExisting() { run("Reading the iPod's existing library", ["rebuild"]) }   // imports, then saves the catalog

    // MARK: safe eject

    enum EjectState: Equatable { case idle, waiting, ejecting, ejected(String), failed(String) }

    /// Never ejects mid-write: if the iPod is busy it waits and ejects the moment the job ends.
    func safeEject() {
        guard volume != nil else { return }
        switch ejectState { case .waiting, .ejecting: return; default: break }
        if runner.running { ejectState = .waiting; return }
        doEject()
    }

    func cancelEject() { if ejectState == .waiting { ejectState = .idle } }

    private func doEject() {
        guard let vol = volume else { return }
        ejectState = .ejecting
        let name = URL(fileURLWithPath: vol).lastPathComponent
        let waiting = pendingCount
        Task.detached {
            Darwin.sync()                                   // flush every pending write to the disk
            var lastErr = ""
            for attempt in 1...4 {
                let (code, out) = Tools.capture("/usr/sbin/diskutil", ["eject", vol], timeout: 90)
                if code == 0 || !FileManager.default.fileExists(atPath: vol) {
                    await MainActor.run {
                        self.ejectState = .ejected(name)
                        self.lastMessage = waiting > 0 ? "\(waiting) song(s) are still waiting and will copy next time you plug it in." : ""
                        self.detect()
                        self.notifyEjected(name)
                    }
                    return
                }
                lastErr = out.trimmingCharacters(in: .whitespacesAndNewlines)
                if attempt < 4 { Darwin.sync(); sleep(2) }
            }
            // still busy: name the apps holding files open on it
            let (_, pids) = Tools.capture("/usr/sbin/lsof", ["-t", "+f", "--", vol], timeout: 30)
            let ids = Set(pids.split(separator: "\n").map(String.init)).filter { $0 != String(getpid()) }
            var apps: [String] = []
            if !ids.isEmpty {
                let (_, names) = Tools.capture("/bin/ps", ["-o", "comm=", "-p", ids.joined(separator: ",")], timeout: 10)
                apps = Array(Set(names.split(separator: "\n").map { URL(fileURLWithPath: String($0)).lastPathComponent })).sorted()
            }
            let msg = apps.isEmpty
                ? "macOS says the iPod is still in use (\(lastErr)). Close any Finder windows showing it and try again. Don't unplug it yet."
                : "It's still in use by: \(apps.joined(separator: ", ")). Quit or close those, then try again. Don't unplug it yet."
            await MainActor.run { self.ejectState = .failed(msg) }
        }
    }

    private func notifyEjected(_ name: String) {
        guard !NSApp.isActive, Bundle.main.bundleIdentifier != nil else { return }
        let c = UNMutableNotificationContent()
        c.title = "Safe to unplug \(name)"
        c.body = "Then hold Menu + centre button for about 8 seconds so it re-reads its library."
        UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: "eject", content: c, trigger: nil))
    }
}

extension Notification.Name { static let showIPod = Notification.Name("showIPod") }

// MARK: - view

struct IPodView: View {
    @ObservedObject var pod = IPod.shared
    @AppStorage("ipodFormat") private var format = "aac"
    @AppStorage("ipodBitrate") private var bitrate = 256
    @State private var search = ""
    @State private var selection = Set<String>()
    @State private var playlist: String?          // nil = whole library
    @State private var newPlaylist = ""
    @State private var addPlaylist = ""
    @State private var confirmRemove = false
    @State private var confirmDelete: String?
    @State private var renaming: String?
    @State private var renameText = ""
    @State private var dropHover = false
    @State private var showLog = false
    @State private var sortOrder: [KeyPathComparator<PodTrack>] = []
    @State private var askNew = false
    @State private var newName = ""

    var shown: [PodTrack] {
        var list = pod.tracks
        if playlist == PlayCounts.mostPlayed {
            list = pod.tracks.filter { $0.plays > 0 }.sorted { ($0.plays, $0.lastPlayedValue) > ($1.plays, $1.lastPlayedValue) }
        } else if let p = pod.playlists.first(where: { $0.name == playlist }) {
            let byID = Dictionary(pod.tracks.map { ($0.id, $0) }, uniquingKeysWith: { a, _ in a })
            list = p.dbids.compactMap { byID[$0] }
        }
        if !sortOrder.isEmpty { list.sort(using: sortOrder) }
        guard !search.isEmpty else { return list }
        return list.filter { "\($0.artist) \($0.title) \($0.album)".localizedCaseInsensitiveContains(search) }
    }

    var body: some View {
        Group {
            if pod.volume == nil { noDevice } else { device }
        }
        .onAppear { pod.detect() }
    }

    var noDevice: some View {
        VStack(spacing: 14) {
            if case .ejected(let name) = pod.ejectState {
                Image(systemName: "checkmark.circle.fill").font(.system(size: 64)).foregroundStyle(.green)
                Text("Safe to unplug \(name)").font(.title2.bold())
                Text("After unplugging, hold Menu + centre button for about 8 seconds\nso the iPod restarts and re-reads its library.")
                    .multilineTextAlignment(.center).foregroundStyle(.secondary)
            } else {
                Image(systemName: "ipod").font(.system(size: 64)).foregroundStyle(.secondary)
                Text("No iPod connected").font(.title2.bold())
                Text("Plug in your iPod and wait for it to appear in Finder.\nIt shows up here automatically.")
                    .multilineTextAlignment(.center).foregroundStyle(.secondary)
            }
            if pod.pendingCount > 0 {
                Label("\(pod.pendingCount) downloaded song(s) will be copied when you plug it in", systemImage: "clock.arrow.circlepath")
                    .foregroundStyle(Color.accentColor)
            }
            if !pod.lastMessage.isEmpty { Text(pod.lastMessage).font(.callout).foregroundStyle(.secondary).frame(maxWidth: 420) }
            Button("Look again") { pod.detect() }
        }.frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    var device: some View {
        HStack(spacing: 0) {
            playlistColumn.frame(width: 210)
            Divider()
            mainColumn.frame(maxWidth: .infinity)
        }
        .searchable(text: $search, placement: .toolbar, prompt: "Search the iPod")
        .alert("Couldn't eject the iPod", isPresented: Binding(get: { if case .failed = pod.ejectState { true } else { false } },
                                                             set: { if !$0 { pod.ejectState = .idle } })) {
            Button("Try Again") { pod.ejectState = .idle; pod.safeEject() }
            Button("OK", role: .cancel) {}
        } message: { if case .failed(let m) = pod.ejectState { Text(m) } }
        .alert("New playlist", isPresented: $askNew) {
            TextField("Playlist name", text: $newName)
            Button("Create") {
                let n = newName.trimmingCharacters(in: .whitespaces)
                if !n.isEmpty { pod.addToPlaylist(n, selection) }
            }
            Button("Cancel", role: .cancel) {}
        } message: { Text("The \(selection.count) selected song(s) go into it.") }
        .modifier(PodDialogs(pod: pod, selection: $selection, playlist: $playlist, confirmRemove: $confirmRemove,
                             confirmDelete: $confirmDelete, renaming: $renaming, renameText: $renameText))
    }

    var mainColumn: some View {
        VStack(spacing: 0) {
            statusBar
            Divider()
            if pod.needsImport { importPrompt } else { trackTable }
            Divider()
            addBar
            if pod.runner.running || showLog { LogView(runner: pod.runner).frame(height: 150) }
        }
        .onDrop(of: [.fileURL], isTargeted: $dropHover) { providers in
            loadDropped(providers); return true
        }
        .overlay {
            if dropHover { RoundedRectangle(cornerRadius: 10).stroke(Color.accentColor, lineWidth: 3).padding(4) }
        }
    }

    var importPrompt: some View {
        VStack(spacing: 10) {
            Text("This iPod hasn't been opened by the app before.").font(.headline)
            Text("Read its current library first so nothing already on it is lost.").foregroundStyle(.secondary)
            Button("Read iPod library") { pod.importExisting() }.disabled(pod.runner.running)
        }.frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    /// One sidebar row. Plain buttons instead of List selection: List rows outside a Section couldn't be clicked.
    private func sidebarRow(_ title: String, _ icon: String, _ value: String?) -> some View {
        let on = playlist == value
        return Button { playlist = value } label: {
            Label(title, systemImage: icon)
                .lineLimit(2)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.vertical, 5).padding(.horizontal, 8)
                .foregroundStyle(on ? Color.white : Color.primary)
                .background(on ? Color.accentColor : Color.clear, in: RoundedRectangle(cornerRadius: 6))
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    var playlistColumn: some View {
        VStack(spacing: 0) {
            ScrollView {
                VStack(alignment: .leading, spacing: 2) {
                    sidebarRow("All songs (\(pod.tracks.count))", "music.note.list", nil)
                    sidebarRow("Most Played (\(pod.tracks.filter { $0.plays > 0 }.count))", "chart.bar.fill", PlayCounts.mostPlayed)
                    Text("Playlists").font(.caption.bold()).foregroundStyle(.secondary)
                        .padding(.top, 12).padding(.bottom, 2).padding(.leading, 8)
                    ForEach(pod.playlists) { p in
                        sidebarRow("\(p.name) (\(p.dbids.count))", "music.note.list", p.name)
                            .contextMenu {
                                Button("Rename…") { renameText = p.name; renaming = p.name }
                                Button("Delete Playlist…", role: .destructive) { confirmDelete = p.name }
                            }
                    }
                }.padding(8)
            }
            Divider()
            HStack {
                TextField("New playlist", text: $newPlaylist).textFieldStyle(.roundedBorder)
                Button {
                    // an empty playlist can't exist in the catalog, so a new one starts with the selected songs
                    pod.addToPlaylist(newPlaylist, selection); newPlaylist = ""
                } label: { Image(systemName: "plus") }
                .disabled(newPlaylist.trimmingCharacters(in: .whitespaces).isEmpty || selection.isEmpty || pod.runner.running)
                .help("Create a playlist from the selected songs")
            }.padding(8)
        }
    }

    var statusBar: some View {
        HStack(spacing: 14) {
            Image(systemName: "ipod").font(.title2)
            VStack(alignment: .leading, spacing: 2) {
                Text(pod.deviceName).font(.headline)
                Text(URL(fileURLWithPath: pod.volume ?? "").lastPathComponent).font(.caption).foregroundStyle(.secondary)
            }
            if pod.totalBytes > 0 {
                let used = Double(pod.totalBytes - pod.freeBytes) / Double(pod.totalBytes)
                VStack(alignment: .leading, spacing: 3) {
                    ProgressView(value: used).frame(width: 180)
                    Text("\(ByteCountFormatter.string(fromByteCount: pod.freeBytes, countStyle: .file)) free of \(ByteCountFormatter.string(fromByteCount: pod.totalBytes, countStyle: .file))")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            Spacer()
            Menu {
                Button("Rebuild database") { pod.rebuild() }
                Button(showLog ? "Hide log" : "Show log") { showLog.toggle() }
                Button("Open iPod in Finder") { NSWorkspace.shared.open(URL(fileURLWithPath: pod.volume ?? "/Volumes")) }
            } label: { Image(systemName: "ellipsis.circle") }.fixedSize().disabled(pod.runner.running)
            ejectControl
        }
        .padding(10)
    }

    @ViewBuilder var ejectControl: some View {
        switch pod.ejectState {
        case .waiting:
            HStack(spacing: 6) {
                ProgressView().controlSize(.small)
                Text("Ejects when copying finishes").font(.callout)
                Button("Cancel") { pod.cancelEject() }
            }
        case .ejecting:
            HStack(spacing: 6) { ProgressView().controlSize(.small); Text("Ejecting safely…").font(.callout) }
        default:
            Button { pod.safeEject() } label: { Label("Eject", systemImage: "eject") }
                .keyboardShortcut("e", modifiers: .command)
                .help(pod.runner.running ? "Waits for the current copy to finish, then ejects" : "Safely eject the iPod (⌘E)")
        }
    }

    var trackTable: some View {
        Table(shown, selection: $selection, sortOrder: $sortOrder) {
            TableColumn("Title", value: \.title) { t in Text(t.title).lineLimit(1) }
            TableColumn("Artist", value: \.artist) { t in Text(t.artist).lineLimit(1).foregroundStyle(.secondary) }
            TableColumn("Album", value: \.album) { t in Text(t.album).lineLimit(1).foregroundStyle(.secondary) }
            TableColumn("Time", value: \.seconds) { t in Text(String(format: "%d:%02d", t.seconds / 60, t.seconds % 60)).monospacedDigit() }.width(50)
            TableColumn("Plays", value: \.plays) { t in
                Text(t.plays > 0 ? "\(t.plays)" : "–").monospacedDigit().foregroundStyle(t.plays > 0 ? .primary : .tertiary)
            }.width(50)
            TableColumn("Skips", value: \.skips) { t in
                Text(t.skips > 0 ? "\(t.skips)" : "–").monospacedDigit().foregroundStyle(.secondary)
            }.width(45)
            TableColumn("Last Played", value: \.lastPlayedValue) { t in
                Text(t.lastPlayed.map { $0.formatted(date: .abbreviated, time: .shortened) } ?? "").foregroundStyle(.secondary)
            }.width(min: 90, ideal: 140)
            TableColumn("Kind", value: \.kind) { t in Text(t.kind).foregroundStyle(.secondary) }.width(min: 50, ideal: 80)
        }
        .contextMenu(forSelectionType: String.self) { ids in
            Menu("Add to Playlist") {
                ForEach(pod.playlists) { p in Button("\(p.name) (\(p.dbids.count))") { pod.addToPlaylist(p.name, ids) } }
                if !pod.playlists.isEmpty { Divider() }
                Button("New Playlist…") { selection = ids; newName = ""; askNew = true }
            }
            if let p = playlist, p != PlayCounts.mostPlayed {
                Button("Remove from “\(p)”") { pod.removeFromPlaylist(p, ids) }
            }
            Divider()
            Button("Delete from iPod…", role: .destructive) { selection = ids; confirmRemove = true }
        }
        .disabled(pod.runner.running)
    }

    var addBar: some View {
        HStack(spacing: 10) {
            Button { chooseFiles() } label: { Label("Add Music…", systemImage: "plus.circle.fill") }
                .fixedSize()
                .disabled(pod.runner.running)
                .help("Pick songs or folders — or just drop them anywhere on this screen")
            Spacer()
            PlaylistPicker(selection: $addPlaylist, title: "Into").frame(width: 230)
            Picker("", selection: $format) {
                Text("AAC").tag("aac"); Text("AIFF (lossless, big)").tag("aiff"); Text("ALAC (skips on 5G)").tag("alac")
            }.labelsHidden().frame(width: 90).help("Format on the iPod. AAC is recommended for the 5th-gen iPod.")
            if format == "aac" {
                Picker("", selection: $bitrate) {
                    ForEach([192, 256, 320], id: \.self) { Text("\($0) kbps").tag($0) }
                }.labelsHidden().frame(width: 95)
            }
            if pod.pendingCount > 0 {
                Text("\(pod.pendingCount) waiting").font(.caption).foregroundStyle(Color.accentColor)
            }
            if pod.runner.running {
                ProgressView().controlSize(.small)
            } else if !pod.lastMessage.isEmpty {
                Image(systemName: "info.circle").foregroundStyle(.secondary).help(pod.lastMessage)
            }
        }
        .padding(10)
    }

    private func chooseFiles() {
        let p = NSOpenPanel()
        p.allowsMultipleSelection = true; p.canChooseDirectories = true; p.canChooseFiles = true
        p.directoryURL = URL(fileURLWithPath: Pref.out)
        p.prompt = "Add to iPod"
        if p.runModal() == .OK { pod.add(paths: p.urls.map(\.path), playlist: addPlaylist) }
    }

    private func loadDropped(_ providers: [NSItemProvider]) {
        var paths: [String] = []
        let group = DispatchGroup()
        let lock = NSLock()
        for pr in providers {
            group.enter()
            _ = pr.loadObject(ofClass: URL.self) { url, _ in
                if let url { lock.lock(); paths.append(url.path); lock.unlock() }
                group.leave()
            }
        }
        group.notify(queue: .main) { pod.add(paths: paths, playlist: addPlaylist) }
    }
}

struct PodDialogs: ViewModifier {
    @ObservedObject var pod: IPod
    @Binding var selection: Set<String>
    @Binding var playlist: String?
    @Binding var confirmRemove: Bool
    @Binding var confirmDelete: String?
    @Binding var renaming: String?
    @Binding var renameText: String

    func body(content: Content) -> some View {
        let deleting = Binding(get: { confirmDelete != nil }, set: { if !$0 { confirmDelete = nil } })
        let renamingOn = Binding(get: { renaming != nil }, set: { if !$0 { renaming = nil } })
        return content
            .confirmationDialog("Delete \(selection.count) track(s) from the iPod?", isPresented: $confirmRemove) {
                Button("Delete from iPod", role: .destructive) { pod.remove(selection); selection.removeAll() }
            } message: { Text("The songs are removed from the iPod and from its playlists. Files on your Mac are not touched.") }
            .confirmationDialog("Delete playlist “\(confirmDelete ?? "")”?", isPresented: deleting) {
                Button("Delete Playlist", role: .destructive) {
                    if let n = confirmDelete { pod.deletePlaylist(n); if playlist == n { playlist = nil } }
                }
            } message: { Text("Only the playlist goes. Every song stays on the iPod.") }
            .alert("Rename playlist", isPresented: renamingOn) {
                TextField("Name", text: $renameText)
                Button("Rename") {
                    if let o = renaming, !renameText.isEmpty {
                        pod.renamePlaylist(o, to: renameText)
                        if playlist == o { playlist = renameText }
                    }
                }
                Button("Cancel", role: .cancel) {}
            }
    }
}

/// Picks one of the iPod's playlists, none, or a brand-new one.
/// `selection`: "" = no playlist, "@auto" = named after the YouTube playlist, else a playlist name.
struct PlaylistPicker: View {
    @Binding var selection: String
    var title = "Playlist"
    var allowAuto = false
    @ObservedObject var pod = IPod.shared
    @State private var askNew = false
    @State private var newName = ""
    @State private var created: [String] = []

    var names: [String] {
        var n = pod.knownPlaylists
        for c in created + [selection] where !c.isEmpty && c != "@auto" && c != "@new" && !n.contains(c) { n.append(c) }
        return n
    }

    var body: some View {
        Picker(title, selection: Binding(get: { selection }, set: { v in
            if v == "@new" { newName = ""; askNew = true } else { selection = v }
        })) {
            if allowAuto { Text("Same name as the YouTube playlist").tag("@auto") }
            Text("No playlist").tag("")
            if !names.isEmpty {
                Divider()
                ForEach(names, id: \.self) { n in
                    let count = pod.playlists.first(where: { $0.name == n })?.dbids.count
                    Text(count.map { "\(n) (\($0))" } ?? (pod.knownPlaylists.contains(n) ? n : "\(n) (new)")).tag(n)
                }
            }
            Divider()
            Text("New Playlist…").tag("@new")
        }
        .help(pod.volume == nil && !pod.knownPlaylists.isEmpty ? "Playlists remembered from the last time the iPod was connected" : "")
        .alert("New playlist", isPresented: $askNew) {
            TextField("Playlist name", text: $newName)
            Button("OK") {
                let n = newName.trimmingCharacters(in: .whitespaces)
                if !n.isEmpty { created.append(n); selection = n }
            }
            Button("Cancel", role: .cancel) {}
        } message: { Text("It's created on the iPod when the songs are copied.") }
    }
}
