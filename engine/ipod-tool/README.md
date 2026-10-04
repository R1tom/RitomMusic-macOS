# ipod-tool

Put music of any format onto a click-wheel iPod from the command line — no iTunes, no Music app.

Written for a 5th-generation iPod (30 GB). It converts whatever you throw at it, copies the
files onto the device, and rewrites the iPod's own music database so the tracks actually show up.

## Requirements

- `python3` (already on your Mac)
- `ffmpeg` — `brew install ffmpeg`
- On macOS it automatically uses **Apple's own AAC encoder** (`aac_at` / `afconvert`), the same
  one iTunes uses. That matters: ffmpeg's built-in AAC encoder produced audible artefacts.

## Usage

Plug the iPod in and wait for it to appear in Finder, then:

```bash
# add a folder, a single file, or a mix — any format ffmpeg can read
bash ~/Music/FLAC/ipod-tool/ipod.sh add ~/Downloads/new-album

# add and put the new tracks in a playlist (created if it doesn't exist,
# appended to if it does)
bash ~/Music/FLAC/ipod-tool/ipod.sh add ~/Music/FLAC/Keethan --playlist "Keethan"

# see what's on it
bash ~/Music/FLAC/ipod-tool/ipod.sh info
bash ~/Music/FLAC/ipod-tool/ipod.sh list --search zubeen

# delete tracks matching some text (asks first)
bash ~/Music/FLAC/ipod-tool/ipod.sh remove --search "test track"

# playlists — these only ever change playlists, never your songs
bash ~/Music/FLAC/ipod-tool/ipod.sh playlist list
bash ~/Music/FLAC/ipod-tool/ipod.sh playlist create "Assamese" --search zubeen
bash ~/Music/FLAC/ipod-tool/ipod.sh playlist add "Assamese" --search "papon"
bash ~/Music/FLAC/ipod-tool/ipod.sh playlist show "Assamese"
bash ~/Music/FLAC/ipod-tool/ipod.sh playlist remove "Assamese" --search "live version"
bash ~/Music/FLAC/ipod-tool/ipod.sh playlist delete "Assamese"

# regenerate the database (harmless, run it any time something looks off)
bash ~/Music/FLAC/ipod-tool/ipod.sh rebuild
```

When it finishes: **eject in Finder**, unplug, then reset the iPod (hold **Menu + centre**
for ~8 seconds) so the firmware re-reads the database.

## Formats

| option | what you get | notes |
|---|---|---|
| `--format aac` *(default)* | 256 kbps AAC, Apple encoder | transparent in practice, ~6 MB per track |
| `--format alac` | Apple Lossless | **causes track-skipping on the 5th-gen iPod** — the CPU can't decode it across a track change. Only use after a flash mod. |
| `--format aiff` | uncompressed lossless | no decoding needed, so it plays fine, but ~10.6 MB per minute |

Change the AAC bitrate with `--bitrate 192` (or 320, etc.).

## Playlists

`playlist create` and `playlist add` both take `--search`, which matches against artist, title,
album, album artist and genre — so `--search zubeen` grabs everything by Zubeen Garg. `add` on a
name that doesn't exist yet creates it, and on one that does appends to it, skipping tracks that
are already in it.

Playlist commands **never touch your audio files or your other playlists**. Deleting a playlist
removes only the playlist; every song stays on the iPod. Removing a track with `remove` does
delete the file, and also takes it out of any playlists it was in.

Playlists live in the catalog alongside the tracks, so they survive `rebuild` and every later
`add`.

## How it decides what to add

Each track remembers the file it came from, so re-running `add` on the same folder skips
what's already there. Use `--duplicates` to force a second copy.

The library lives in `<iPod>/.ipod_sync/catalog.json`, so it travels with the device — plug the
iPod into another Mac with this tool and everything is still known. The previous database is
backed up to `<iPod>/.ipod_sync/backups/` before every write (last 5 kept).

On first run it imports whatever is already on the iPod, so nothing is lost.

## Known limitations

- **Album art for newly added tracks is not generated.** Art already on the iPod is preserved
  (the tool keeps each track's identifier and leaves the artwork database alone), but new tracks
  will show the generic music icon. Adding artwork means writing Apple's separate `ArtworkDB`
  and thumbnail files, which isn't implemented here.
- Video, podcasts, audiobooks and ratings/play counts aren't managed — this is a music tool.
- Smart playlists, ratings and play counts aren't managed.

## If something goes wrong

Restore a previous database from `<iPod>/.ipod_sync/backups/` by copying it over
`<iPod>/iPod_Control/iTunes/iTunesDB`, then reset the iPod.

If the iPod ever unmounts mid-run, just plug it back in and run the same command again —
finished files are skipped.
