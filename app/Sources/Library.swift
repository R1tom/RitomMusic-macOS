import Foundation
import AppKit
import SwiftUI

struct Song: Identifiable, Hashable {
    var id: String { path }
    let path: String
    let name: String
    let ext: String
    let folder: String
    let size: Int64
    let date: Date
    var isLossless: Bool { ext == "flac" }
}

@MainActor
final class Library: ObservableObject {
    static let shared = Library()
    nonisolated static let audioExts: Set<String> = ["flac", "m4a", "opus", "mp3", "ogg", "webm", "aac", "wav", "aiff", "aif"]

    @Published var songs: [Song] = []
    @Published var scanning = false
    @Published var failedIDs: [(id: String, title: String)] = []
    @Published var manifestCounts: [String: Int] = [:]
    let tools = Runner()
    @Published var toolTitle = ""

    private var pendingScan = false

    func rescanSoon() {
        guard !pendingScan else { return }
        pendingScan = true
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak self] in
            self?.pendingScan = false
            self?.scan()
        }
    }

    func scan() {
        let root = Pref.out
        scanning = true
        Task.detached(priority: .userInitiated) {
            let songs = Self.walk(root)
            let (failed, counts) = Self.readManifest(root)
            await MainActor.run {
                self.songs = songs
                self.failedIDs = failed
                self.manifestCounts = counts
                self.scanning = false
            }
        }
    }

    nonisolated static func walk(_ root: String) -> [Song] {
        let fm = FileManager.default
        let base = URL(fileURLWithPath: root)
        guard let en = fm.enumerator(at: base, includingPropertiesForKeys: [.fileSizeKey, .contentModificationDateKey, .isRegularFileKey],
                                     options: [.skipsHiddenFiles, .skipsPackageDescendants]) else { return [] }
        var out: [Song] = []
        for case let u as URL in en {
            let ext = u.pathExtension.lowercased()
            guard audioExts.contains(ext) else { continue }
            let v = try? u.resourceValues(forKeys: [.fileSizeKey, .contentModificationDateKey, .isRegularFileKey])
            guard v?.isRegularFile == true else { continue }
            let rel = u.deletingLastPathComponent().path.replacingOccurrences(of: base.path, with: "")
            out.append(Song(path: u.path, name: u.deletingPathExtension().lastPathComponent, ext: ext,
                            folder: rel.isEmpty ? "" : String(rel.dropFirst()),
                            size: Int64(v?.fileSize ?? 0), date: v?.contentModificationDate ?? .distantPast))
        }
        return out.sorted { $0.date > $1.date }
    }

    nonisolated static func readManifest(_ root: String) -> ([(id: String, title: String)], [String: Int]) {
        guard let d = FileManager.default.contents(atPath: root + "/.ytflac-manifest.json"),
              let obj = try? JSONSerialization.jsonObject(with: d) as? [String: Any] else { return ([], [:]) }
        var failed: [(String, String)] = []
        var counts: [String: Int] = [:]
        for (id, v) in obj {
            guard let e = v as? [String: Any] else { continue }
            let st = e["status"] as? String ?? "?"
            counts[st, default: 0] += 1
            if st != "done" && st != "yt-fallback" { failed.append((id, e["title"] as? String ?? id)) }
        }
        return (failed.sorted { $0.1 < $1.1 }.map { (id: $0.0, title: $0.1) }, counts)
    }

    func runTool(_ title: String, _ args: [String]) {
        guard !tools.running else { return }
        toolTitle = title
        tools.clear()
        tools.onExit = { [weak self] _, _ in self?.rescanSoon() }
        tools.start(Tools.python, args)
    }

    func checkCovers() { runTool("Check cover art", [Tools.ytflac, "-o", Pref.out, "--check-covers"]) }
    func fixCovers() { runTool("Fix missing cover art", [Tools.ytflac, "-o", Pref.out, "--fix-covers"]) }
    func verify(deep: Bool) {
        runTool(deep ? "Verify downloads (with YouTube lengths)" : "Verify downloads",
                [Tools.verify, Pref.out] + (deep ? ["--deep"] : []))
    }

    func trash(_ items: [Song]) {
        for s in items { try? FileManager.default.trashItem(at: URL(fileURLWithPath: s.path), resultingItemURL: nil) }
        songs.removeAll { s in items.contains(s) }
    }
}

// MARK: - view

struct LibraryView: View {
    @ObservedObject var lib = Library.shared
    @ObservedObject var ipod = IPod.shared
    @State private var search = ""
    @State private var filter = 0            // 0 all, 1 flac, 2 youtube audio
    @State private var selection = Set<String>()
    @State private var sortOrder = [KeyPathComparator(\Song.date, order: .reverse)]
    @State private var showTool = false
    @State private var confirmTrash = false
    @State private var showFailed = false
    @State private var askNew = false
    @State private var newName = ""
    @State private var sendPaths: [String] = []

    var filtered: [Song] {
        lib.songs.filter { s in
            (filter == 0 || (filter == 1) == s.isLossless) &&
            (search.isEmpty || s.name.localizedCaseInsensitiveContains(search) || s.folder.localizedCaseInsensitiveContains(search))
        }.sorted(using: sortOrder)
    }
    var selected: [Song] { lib.songs.filter { selection.contains($0.path) } }

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            Table(filtered, selection: $selection, sortOrder: $sortOrder) {
                TableColumn("Song", value: \.name) { s in
                    HStack(spacing: 6) {
                        Image(systemName: s.isLossless ? "waveform" : "play.rectangle")
                            .foregroundStyle(s.isLossless ? Color.accentColor : .orange)
                        Text(s.name).lineLimit(1)
                    }
                }
                TableColumn("Type", value: \.ext) { s in
                    Text(s.isLossless ? "FLAC" : s.ext.uppercased())
                        .font(.caption.weight(.semibold))
                        .padding(.horizontal, 6).padding(.vertical, 1)
                        .background((s.isLossless ? Color.accentColor : Color.orange).opacity(0.15), in: Capsule())
                }.width(70)
                TableColumn("Folder", value: \.folder) { s in Text(s.folder).foregroundStyle(.secondary) }.width(min: 60, ideal: 90)
                TableColumn("Size", value: \.size) { s in
                    Text(ByteCountFormatter.string(fromByteCount: s.size, countStyle: .file)).monospacedDigit()
                }.width(80)
                TableColumn("Added", value: \.date) { s in
                    Text(s.date, format: .dateTime.day().month().year()).foregroundStyle(.secondary)
                }.width(100)
            }
            .contextMenu(forSelectionType: String.self) { ids in
                let items = lib.songs.filter { ids.contains($0.path) }
                Button("Play") { items.forEach { NSWorkspace.shared.open(URL(fileURLWithPath: $0.path)) } }
                Button("Show in Finder") { NSWorkspace.shared.activateFileViewerSelecting(items.map { URL(fileURLWithPath: $0.path) }) }
                Divider()
                sendMenu(items.map(\.path))
                Divider()
                Button("Move to Trash…", role: .destructive) { selection = ids; confirmTrash = true }
            } primaryAction: { ids in
                lib.songs.filter { ids.contains($0.path) }.forEach { NSWorkspace.shared.open(URL(fileURLWithPath: $0.path)) }
            }
            Divider()
            footer
        }
        .searchable(text: $search, placement: .toolbar, prompt: "Search songs")
        .onAppear { if lib.songs.isEmpty { lib.scan() } }
        .sheet(isPresented: $showTool) { ToolSheet(runner: lib.tools, title: lib.toolTitle) }
        .sheet(isPresented: $showFailed) { FailedSheet(dismiss: { showFailed = false }) }
        .alert("New iPod playlist", isPresented: $askNew) {
            TextField("Playlist name", text: $newName)
            Button("Send") {
                let n = newName.trimmingCharacters(in: .whitespaces)
                ipod.send(paths: sendPaths, playlist: n.isEmpty ? nil : n)
            }
            Button("Cancel", role: .cancel) {}
        } message: { Text("\(sendPaths.count) song(s) go into a new playlist on the iPod.") }
        .confirmationDialog("Move \(selection.count) song(s) to the Trash?", isPresented: $confirmTrash) {
            Button("Move to Trash", role: .destructive) { lib.trash(selected); selection.removeAll() }
        } message: { Text("You can put them back from the Trash in Finder.") }
    }

    /// "Send to iPod" with every playlist on the iPod, plus a new one.
    func sendMenu(_ paths: [String]) -> some View {
        Menu("Send to iPod") {
            Button("Songs only (no playlist)") { ipod.send(paths: paths, playlist: nil) }
            let names = ipod.knownPlaylists
            if !names.isEmpty {
                Section(ipod.volume == nil ? "Playlists (remembered)" : "Add to playlist") {
                    ForEach(names, id: \.self) { n in
                        let c = ipod.playlists.first(where: { $0.name == n })?.dbids.count
                        Button(c.map { "\(n) (\($0))" } ?? n) { ipod.send(paths: paths, playlist: n) }
                    }
                }
            }
            Divider()
            Button("New Playlist…") { sendPaths = paths; newName = ""; askNew = true }
        }
        .help(ipod.volume == nil ? "The iPod isn't plugged in — songs are copied as soon as it is" : "Convert and copy to the iPod")
    }

    var header: some View {
        HStack(spacing: 12) {
            Picker("", selection: $filter) {
                Text("All").tag(0); Text("Lossless FLAC").tag(1); Text("YouTube audio").tag(2)
            }.pickerStyle(.segmented).frame(width: 320).labelsHidden()
            Spacer()
            if !lib.failedIDs.isEmpty {
                Button { showFailed = true } label: {
                    Label("\(lib.failedIDs.count) failed", systemImage: "exclamationmark.triangle")
                }.tint(.orange)
            }
            Menu {
                Button("Check cover art") { lib.checkCovers(); showTool = true }
                Button("Fix missing cover art") { lib.fixCovers(); showTool = true }
                Divider()
                Button("Verify downloads") { lib.verify(deep: false); showTool = true }
                Button("Verify downloads (also compare lengths with YouTube)") { lib.verify(deep: true); showTool = true }
            } label: { Label("Tools", systemImage: "wrench.and.screwdriver") }
                .fixedSize()
                .disabled(lib.tools.running)
            Button { lib.scan() } label: { Image(systemName: "arrow.clockwise") }.help("Rescan the folder")
            Button { NSWorkspace.shared.open(URL(fileURLWithPath: Pref.out)) } label: { Image(systemName: "folder") }
                .help("Open the music folder in Finder")
        }
        .padding(10)
    }

    var footer: some View {
        let flac = lib.songs.filter(\.isLossless).count
        let bytes = lib.songs.reduce(Int64(0)) { $0 + $1.size }
        return HStack {
            if lib.scanning { ProgressView().controlSize(.small) }
            Text("\(lib.songs.count) songs · \(flac) lossless · \(lib.songs.count - flac) YouTube audio · \(ByteCountFormatter.string(fromByteCount: bytes, countStyle: .file))")
            Spacer()
            if !selection.isEmpty {
                Text("\(selection.count) selected")
                sendMenu(selected.map(\.path)).fixedSize()
            }
            if lib.tools.running {
                Button { showTool = true } label: { Label(lib.toolTitle, systemImage: "gearshape.2") }
            }
            Text(Pref.out.replacingOccurrences(of: NSHomeDirectory(), with: "~")).foregroundStyle(.secondary)
        }
        .font(.callout)
        .padding(.horizontal, 12).padding(.vertical, 7)
    }
}

struct FailedSheet: View {
    @ObservedObject var lib = Library.shared
    var dismiss: () -> Void
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Tracks that failed").font(.title2.bold())
            Text("These songs couldn't be downloaded from any service or from YouTube. Retrying often works once the service recovers.")
                .foregroundStyle(.secondary)
            List(lib.failedIDs, id: \.id) { f in
                HStack {
                    Text(f.title).lineLimit(1)
                    Spacer()
                    Link(destination: URL(string: "https://www.youtube.com/watch?v=\(f.id)")!) { Image(systemName: "arrow.up.right.square") }
                }
            }.frame(minHeight: 240)
            HStack {
                Spacer()
                Button("Close") { dismiss() }
                Button("Retry All") {
                    Downloader.shared.retryFailed(ids: lib.failedIDs.map(\.id))
                    NotificationCenter.default.post(name: .showDownloads, object: nil)
                    dismiss()
                }.keyboardShortcut(.defaultAction)
            }
        }
        .padding(20).frame(width: 560, height: 440)
    }
}

extension Notification.Name {
    static let showDownloads = Notification.Name("showDownloads")
}
