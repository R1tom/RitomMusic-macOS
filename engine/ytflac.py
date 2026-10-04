#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ytflac — download true-lossless FLAC for the songs in a YouTube link.

It does NOT rip YouTube's lossy audio. Instead it:
  1. reads the YouTube song / playlist URL with yt-dlp (metadata only),
  2. matches each track to Spotify (Odesli by exact URL, then Spotify search),
  3. hands the Spotify track to SpotiFLAC, which downloads the real FLAC from
     Tidal / Qobuz / Deezer / Amazon Music.

So the audio you get is the studio master from a lossless service, tagged and
saved as .flac — not a re-encode of the YouTube stream.

--------------------------------------------------------------------------
REQUIREMENTS  (install once)
--------------------------------------------------------------------------
  yt-dlp + ffmpeg        brew install yt-dlp ffmpeg

  SpotiFLAC engine — use the MAINTAINED module version (same engine as the
  SpotiFLAC desktop app, with current service endpoints):

      pip3 install --break-system-packages \
          "git+https://github.com/ShuShuzinhuu/SpotiFLAC-Module-Version" nodriver
      export SPOTIFLAC="python3 -m SpotiFLAC"

  (The older jelte1/SpotiFLAC-Command-Line-Interface fork also works, but its
  Qobuz/Deezer/Tidal endpoints have gone stale — expect auth errors there.)

Point ytflac at SpotiFLAC one of these ways:
  * export SPOTIFLAC="python3 -m SpotiFLAC"              (recommended, after pip install)
  * export SPOTIFLAC="python3 /path/to/launcher.py"      (a cloned repo)
  * export SPOTIFLAC="/path/to/SpotiFLAC-macOS"          (prebuilt binary)
  * or pass  --spotiflac "..."
  * or just have a `spotiflac` binary on PATH / the module pip-installed —
    ytflac auto-detects both.

--------------------------------------------------------------------------
USAGE
--------------------------------------------------------------------------
  python3 ytflac.py "https://www.youtube.com/watch?v=XXXX"          # one song
  python3 ytflac.py "https://www.youtube.com/playlist?list=PLXXXX"  # whole playlist
  python3 ytflac.py --dry-run "https://youtu.be/XXXX"               # match only, no download
  python3 ytflac.py -o ~/Music/rips --service qobuz tidal "URL"

Run  python3 ytflac.py -h  for all options.
"""

import argparse
import base64
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

VERSION = "1.0"

# Public Spotify "client-credentials" app id/secret shipped with the open-source
# SpotiFLAC CLI. Used ONLY to look up public track metadata for matching (no user
# login, no personal data). Override with your own via env if these ever get
# rate-limited or rotated:  SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET
_DEF_ID = base64.b64decode("ODNlNDQzMGI0NzAwNDM0YmFhMjEyMjhhOWM3ZDExYzU=").decode()
_DEF_SECRET = base64.b64decode("OWJiOWUxMzFmZjI4NDI0Y2I2YTQyMGFmZGY0MWQ0NGE=").decode()

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


# ---------------------------------------------------------------- pretty output
def _c(code):
    return code if sys.stdout.isatty() else ""


CINFO, CWARN, CERR, CGOOD, COFF = (
    _c("\033[1;34m"), _c("\033[1;33m"), _c("\033[1;31m"), _c("\033[1;32m"), _c("\033[0m"))


def info(msg):
    print("%s==>%s %s" % (CINFO, COFF, msg))


def good(msg):
    print("%s ok %s %s" % (CGOOD, COFF, msg))


def warn(msg):
    print("%swarn%s %s" % (CWARN, COFF, msg), file=sys.stderr)


def die(msg, code=1):
    print("%serror%s %s" % (CERR, COFF, msg), file=sys.stderr)
    sys.exit(code)


# ---------------------------------------------------------------- small http
def http_json(url, data=None, headers=None, timeout=25):
    """GET/POST returning (status, parsed_json_or_none). Never raises on HTTP errors."""
    hdrs = {"User-Agent": UA, "Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode()
        hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
    req = urllib.request.Request(url, data=body, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(raw)
            except ValueError:
                return r.status, None
    except urllib.error.HTTPError as e:
        retry = e.headers.get("Retry-After") if e.headers else None
        try:
            payload = json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            payload = None
        return e.code, {"_retry_after": retry, "_body": payload}
    except Exception as e:
        return 0, {"_err": str(e)}


# ---------------------------------------------------------------- text helpers
# Multi-word noise phrases removed anywhere in a segment (order matters: longest first)
_NOISE_PHRASES = [
    "official music video", "official lyric video", "official lyrics video",
    "official video song", "official full video", "official music", "official video",
    "official audio", "official visualizer", "official visualiser", "official trailer",
    "full video song", "full audio song", "full song video", "video song hd",
    "music video", "lyric video", "lyrical video", "video song", "audio song",
    "full video", "full audio", "full song", "with lyrics", "official lyrics",
    "slowed and reverb", "slowed reverb", "slowed + reverb", "8d audio",
    "reprise version", "unplugged version", "hd video", "4k video", "hq audio",
]
# Bare trailing tokens stripped iteratively (only while >1 word remains)
_NOISE_WORDS = ("video", "audio", "song", "hd", "4k", "hq", "mv", "official", "hindi")
# Music labels / distributors that show up as pipe segments (never the song/album)
_LABELS = ("t-series", "tseries", "t series", "zee music", "zeemusic", "sony music",
           "sony music india", "yrf", "yrf music", "tips", "tips official", "tips music",
           "saregama", "speed records", "times music", "eros", "eros now", "venus",
           "shemaroo", "aditya music", "lahari music", "muzik247", "believe",
           "vevo", "records", "music company")
_QUOTE_RE = re.compile(r'[“”„‟"“”]|[\'‘’‘’]')


def _fix_quotes(t):
    return (t or "").replace("“", '"').replace("”", '"') \
        .replace("‘", "'").replace("’", "'")


def _strip_noise(seg):
    """Remove marketing noise from one title segment, keep the real name."""
    s = _fix_quotes(seg).strip().strip('"\'')
    # strip parenthetical noise like "(HD)" "(Official Audio)" first
    s = re.sub(r"[\(\[]\s*(?:%s)[^\)\]]*[\)\]]" % "|".join(_NOISE_WORDS), " ", s, flags=re.I)
    for ph in _NOISE_PHRASES:
        s = re.sub(r"\b%s\b" % re.escape(ph), " ", s, flags=re.I)
    # collapse any brackets left empty by the removals above: "( )" "[]" etc.
    for _ in range(3):
        s = re.sub(r"[\(\[]\s*[\)\]]", " ", s)
    s = re.sub(r"\s{2,}", " ", s).strip(" -|—–·:•\"'()[]").strip()
    # iteratively drop trailing bare noise words while more than one word remains
    changed = True
    while changed and len(s.split()) > 1:
        changed = False
        for w in _NOISE_WORDS:
            m = re.search(r"\s%s$" % w, s, flags=re.I)
            if m:
                candidate = s[:m.start()].strip(" -|—–·:•")
                # 'song' is often part of the real title ("Sailor Song") —
                # only strip it when at least two words would remain
                if w == "song" and len(candidate.split()) < 2:
                    continue
                s = candidate
                changed = True
    return re.sub(r"\s{2,}", " ", s).strip(" -|—–·:•\"'()[]").strip()


def clean_title(t):
    return _strip_noise(t)


def _is_castlist(seg):
    # e.g. "Ranbir Kapoor, Deepika Padukone" — comma-separated Title-Case names
    if "," in seg and re.search(r"[A-Z][a-z]+\s+[A-Z][a-z]+", seg):
        return True
    return bool(re.match(r"^(?:starring|ft\.?|feat\.?|cast)\b", seg.strip(), re.I))


def _is_label(seg):
    s = seg.strip().lower()
    return any(lbl in s for lbl in _LABELS) or len(s) <= 1


def _pick_album(segs):
    """From the pipe-segments after the song, guess the movie/album hint."""
    for seg in segs:
        cand = _strip_noise(seg)
        if not cand or _is_label(seg) or _is_castlist(seg):
            continue
        # skip pure "feat/singer" credit lines that are long name lists
        return cand
    return ""


def parse_track(title, uploader):
    """Best-effort (artist, track, album_hint) from a YouTube title + channel.

    Handles Western 'Artist - Title' AND film formats like
    '"Safarnama" Video Song | Tamasha | Ranbir Kapoor, ... | T-Series'.
    """
    t = _fix_quotes(title or "")
    artist = ""
    up = (uploader or "").strip()
    m = re.match(r"^(.*?)\s*-\s*Topic$", up, re.I)   # YT Music auto-channel
    if m and m.group(1).strip():
        artist = m.group(1).strip()

    # Pull soundtrack attributions '(from "Movie")' / '(From Movie)' out FIRST, as an
    # album hint — otherwise the quoted MOVIE name gets mistaken for the song title
    # (e.g. 'Rama Theme (From "Ramayana")').
    album_paren = ""
    mfrom = re.search(r'[\(\[]\s*from\s+"?([^"\)\]]+?)"?\s*[\)\]]', t, re.I)
    if mfrom:
        album_paren = _strip_noise(mfrom.group(1))
    t = re.sub(r'[\(\[]\s*from\s+[^\)\]]*[\)\]]', " ", t, flags=re.I)
    t = re.sub(r"\s{2,}", " ", t).strip()

    segs = [s.strip() for s in re.split(r"\s*[|•]\s*", t) if s.strip()]

    # 1) A quoted phrase at the START of the title is the exact song name
    #    (e.g. '"Safarnama" Video Song…'). A quote MID-title is usually a movie
    #    reference, not the song — so only anchor at the start of the first segment.
    first_seg = segs[0] if segs else t
    qm = re.match(r'^\s*"([^"]{2,80})"', first_seg) or re.match(r"^\s*'([^']{2,80})'", first_seg)
    if qm:
        track = qm.group(1).strip()
        album = _pick_album(segs[1:]) if len(segs) > 1 else ""
    # 2) Pipe-delimited (typical film song): first segment holds the song.
    elif len(segs) >= 2:
        track = _strip_noise(segs[0])
        album = _pick_album(segs[1:])
    # 3) Western 'Artist - Title'.
    else:
        seg = _strip_noise(segs[0] if segs else t)
        album = ""
        if " - " in seg:
            left, right = seg.split(" - ", 1)
            right_tokens = set(norm(right).split())
            if right_tokens and right_tokens.issubset(_GENERIC):
                # right side is only a qualifier ("Epic Version", "Slowed", "Remix"):
                # the whole thing is the song name and there is no artist here.
                track = re.sub(r"\s+-\s+", " ", seg).strip()
            else:
                if not artist:
                    artist = left.strip()
                track = right.strip()
        else:
            track = seg

    # strip trailing "feat. …" from the track name for a cleaner search
    track = re.sub(r"\s*[\(\[]?\s*feat\.?\s+[^\)\]]*[\)\]]?", "", track, flags=re.I).strip()
    if not qm:  # quoted names are authoritative; don't over-clean them
        track = _strip_noise(track)
    if not album:
        album = album_paren
    return artist, track, album


# Track names made only of these words carry no identity on their own.
_GENERIC = {"epic", "version", "theme", "remix", "cover", "ost", "soundtrack",
            "instrumental", "extended", "edit", "mix", "reprise", "slowed", "reverb",
            "remaster", "remastered", "live", "acoustic", "audio", "video", "song",
            "full", "hd", "4k", "trailer", "music", "the", "x", "and"}


def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


# ---------------------------------------------------------------- yt-dlp
def find_ytdlp():
    for name in ("yt-dlp", "yt-dlp.exe"):
        if which(name):
            return name
    return None


def which(cmd):
    for p in os.environ.get("PATH", "").split(os.pathsep):
        f = os.path.join(p, cmd)
        if os.path.isfile(f) and os.access(f, os.X_OK):
            return f
    return None


def yt_entries(url, yes_playlist, limit):
    """Return list of dicts: {id, url, title, uploader, duration}."""
    ytdlp = find_ytdlp()
    if not ytdlp:
        die("yt-dlp not found. Install:  brew install yt-dlp")
    cmd = [ytdlp, "-J", "--flat-playlist", "--ignore-errors"]
    # A watch URL that also carries &list= is treated as a single video unless
    # the user explicitly asked for the whole playlist.
    if not yes_playlist and re.search(r"[?&]list=", url) and "/playlist" not in url:
        cmd.append("--no-playlist")
    cmd.append(url)
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        die("yt-dlp timed out reading: %s" % url)
    if not out.stdout.strip():
        die("yt-dlp could not read %s\n%s" % (url, out.stderr.strip()[:500]))
    try:
        data = json.loads(out.stdout)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        die("yt-dlp returned no usable data for %s\n%s"
            % (url, out.stderr.strip()[:500]))

    entries = []

    def add(e):
        if not e:
            return
        vid = e.get("id") or ""
        vurl = e.get("webpage_url") or e.get("url") or (
            "https://www.youtube.com/watch?v=%s" % vid if vid else "")
        if vid and not vurl.startswith("http"):
            vurl = "https://www.youtube.com/watch?v=%s" % vid
        entries.append({
            "id": vid,
            "url": vurl,
            "title": e.get("title") or "",
            "uploader": e.get("uploader") or e.get("channel") or e.get("artist") or "",
            "track": e.get("track") or "",
            "artist": e.get("artist") or "",
            "album": e.get("album") or "",
            "duration": e.get("duration") or 0,
        })

    if data.get("_type") == "playlist" or "entries" in data:
        for e in (data.get("entries") or []):
            # nested playlists (rare) -> flatten one level
            if e and e.get("_type") == "playlist":
                for e2 in (e.get("entries") or []):
                    add(e2)
            else:
                add(e)
    else:
        add(data)

    # drop unavailable / private placeholders
    entries = [e for e in entries if e["id"] and e["title"].lower() not in (
        "[deleted video]", "[private video]", "[unavailable video]")]
    if limit and limit > 0:
        entries = entries[:limit]
    return entries, (data.get("title") if data.get("_type") == "playlist" else None)


# ---------------------------------------------------------------- Spotify auth/search
class Spotify:
    def __init__(self):
        self.id = os.environ.get("SPOTIFY_CLIENT_ID", _DEF_ID)
        self.secret = os.environ.get("SPOTIFY_CLIENT_SECRET", _DEF_SECRET)
        self._token = None

    def token(self):
        if self._token:
            return self._token
        auth = base64.b64encode(("%s:%s" % (self.id, self.secret)).encode()).decode()
        st, js = http_json("https://accounts.spotify.com/api/token",
                           data={"grant_type": "client_credentials"},
                           headers={"Authorization": "Basic %s" % auth})
        if st == 200 and js and js.get("access_token"):
            self._token = js["access_token"]
            return self._token
        warn("Spotify token failed (status %s) — search fallback disabled" % st)
        return None

    def _raw(self, tok, q):
        """Search with resilience for long runs: refresh the token when it
        expires mid-batch (401 — client-credentials tokens last ~1h, a big
        playlist takes longer) and back off briefly on rate limits (429)."""
        qs = urllib.parse.urlencode({"q": q, "type": "track", "limit": 10})
        url = "https://api.spotify.com/v1/search?%s" % qs
        for attempt in range(3):
            if not tok:
                return []
            st, js = http_json(url, headers={"Authorization": "Bearer %s" % tok})
            if st == 200 and js:
                return (js.get("tracks") or {}).get("items") or []
            if st == 401:                       # token expired -> refresh once
                self._token = None
                tok = self.token()
                continue
            if st == 429:                       # rate limited -> honor Retry-After
                try:
                    wait = min(int((js or {}).get("_retry_after") or 5), 60)
                except (TypeError, ValueError):
                    wait = 5
                warn("Spotify rate limit — waiting %ss" % wait)
                time.sleep(wait)
                continue
            return []
        return []

    def search(self, artist, track, album, duration):
        tok = self.token()
        if not tok or not track:
            return None
        # Try several queries (specific -> loose). Film songs often have no artist
        # in the YouTube title, so plain title / title+movie queries matter most.
        queries = []
        if artist:
            queries.append('track:"%s" artist:"%s"' % (track, artist))
        if album:
            queries.append("%s %s" % (track, album))
        queries.append('track:"%s"' % track)
        queries.append(track)
        seen = set()
        for q in queries:
            if q in seen:
                continue
            seen.add(q)
            m = best_match(self._raw(tok, q), artist, track, duration, album)
            if m:
                return m
        return None


def best_match(items, artist, track, duration, album=""):
    """Pick the closest Spotify track.

    When we know the artist (Western videos) we require artist OR a near-exact
    title. When we don't (film 'video songs' rarely name the singer) we accept a
    strong title-containment match, using movie/album + duration only to rank.
    """
    want = set(norm(track).split())
    if not want:
        return None
    want_a, want_alb = norm(artist), norm(album)
    scored = []
    for it in items:
        iw = set(norm(it.get("name")).split())
        if not iw:
            continue
        overlap = len(want & iw) / len(want)
        arts = " ".join(norm(a.get("name")) for a in it.get("artists", []))
        alb = norm((it.get("album") or {}).get("name"))
        artist_ok = bool(want_a) and (
            want_a in arts or any(w in arts.split() for w in want_a.split() if len(w) > 2))
        dur_ok = bool(duration) and bool(it.get("duration_ms")) and \
            abs(it["duration_ms"] / 1000.0 - duration) <= 6
        alb_ok = bool(want_alb) and len(want_alb) > 2 and (want_alb in alb or alb in want_alb)
        score = overlap + (0.5 if artist_ok else 0) + (0.35 if dur_ok else 0) + \
            (0.3 if alb_ok else 0)
        scored.append((score, overlap, artist_ok, it))
    if not scored:
        return None
    scored.sort(key=lambda x: x[0], reverse=True)
    score, overlap, artist_ok, best = scored[0]
    # Guard: if the parsed track name is ONLY generic words (e.g. "Epic Version",
    # "Theme", "Remix"), a title-overlap match is meaningless — require a real
    # artist match. Stops 'X - Epic Version' latching onto an unrelated track.
    if want.issubset(_GENERIC) and not artist_ok:
        return None
    if want_a:
        ok = overlap >= 0.6 and (artist_ok or overlap >= 0.85)
    else:
        ok = overlap >= 0.85       # all song tokens present in the Spotify title
    if not ok:
        return None
    imgs = (best.get("album") or {}).get("images") or []
    return {
        "url": best["external_urls"]["spotify"],
        "name": best["name"],
        "artist": ", ".join(a["name"] for a in best["artists"]),
        "isrc": (best.get("external_ids") or {}).get("isrc", ""),
        "art": (imgs[0].get("url") if imgs else "") or "",
    }


# ---------------------------------------------------------------- Odesli / song.link
def odesli(youtube_url, ctx):
    """Resolve a YouTube URL to a Spotify track via Odesli. Used only as a
    fallback when Spotify search misses. Odesli's free API rate-limits hard, so
    the first 429 trips a circuit breaker (ctx['odesli_off']) and we stop trying
    it for the rest of the run rather than crawling through escalating backoffs."""
    if ctx.get("odesli_off"):
        return None
    qs = urllib.parse.urlencode({"url": youtube_url, "userCountry": "US"})
    url = "https://api.song.link/v1-alpha.1/links?%s" % qs
    st, js = http_json(url)
    if st == 200 and js:
        sp = (js.get("linksByPlatform") or {}).get("spotify") or {}
        spurl = sp.get("url")
        if not spurl:
            return None
        ent = (js.get("entitiesByUniqueId") or {}).get(sp.get("entityUniqueId") or "", {})
        return {"url": spurl, "name": ent.get("title", ""),
                "artist": ent.get("artistName", ""), "isrc": "",
                "art": ent.get("thumbnailUrl", "") or ""}
    if st == 429:
        ctx["odesli_off"] = True
        warn("Odesli is rate-limiting — using Spotify search only for the rest of this run.")
    return None


# ---------------------------------------------------------------- SpotiFLAC command
def _split_cmd(s):
    # Split a command string AND expand ~ and $VARS in each token, because the
    # command is run directly (no shell), so "~/foo/launcher.py" would otherwise
    # stay literal and resolve against the cwd.
    return [os.path.expanduser(os.path.expandvars(tok)) for tok in shlex.split(s)]


def find_spotiflac(explicit):
    if explicit:
        return _split_cmd(explicit)
    env = os.environ.get("SPOTIFLAC")
    if env:
        return _split_cmd(env)
    for name in ("spotiflac", "SpotiFLAC", "SpotiFLAC-macOS", "spotiflac-cli"):
        p = which(name)
        if p:
            return [p]
    # pip-installed module version (preferred: maintained endpoints)
    try:
        import importlib.util
        if importlib.util.find_spec("SpotiFLAC") is not None:
            return [sys.executable, "-m", "SpotiFLAC"]
    except Exception:
        pass
    # a cloned launcher.py in common spots (module version first, then old fork)
    for base in (os.path.expanduser("~/SpotiFLAC-Module-Version"),
                 os.path.join(os.getcwd(), "SpotiFLAC-Module-Version"),
                 os.getcwd(), os.path.expanduser("~"),
                 os.path.expanduser("~/SpotiFLAC-Command-Line-Interface"),
                 os.path.join(os.getcwd(), "SpotiFLAC-Command-Line-Interface")):
        cand = os.path.join(base, "launcher.py")
        if os.path.isfile(cand):
            return [sys.executable, cand]
    return None


def _fix_bad_utf8_tree(root):
    """Re-encode any .py file under root that isn't valid UTF-8 (Latin-1 -> UTF-8).

    Repairs the known nodriver 0.50.x wheel bug: cdp/network.py ships a raw
    0xB1 ('±' in Latin-1) inside a comment. Python <=3.11 tolerated invalid
    bytes in comments; Python 3.12+ correctly raises SyntaxError, which crashes
    the whole SpotiFLAC engine at import. Re-encoding is content-preserving.
    """
    fixed = []
    for dp, _dirs, files in os.walk(root):
        for f in files:
            if not f.endswith(".py"):
                continue
            p = os.path.join(dp, f)
            try:
                raw = open(p, "rb").read()
            except OSError:
                continue
            try:
                raw.decode("utf-8")
            except UnicodeDecodeError:
                try:
                    open(p, "wb").write(raw.decode("latin-1").encode("utf-8"))
                    fixed.append(p)
                except OSError as e:
                    warn("could not repair %s (%s)" % (p, e))
    return fixed


def preflight_spotiflac(spcmd):
    """Verify the engine can start at all (cheap --help probe) BEFORE looping
    over tracks. If it crashes with the known non-UTF-8 dependency corruption,
    repair it in place and retry once; otherwise die with the real error."""
    for attempt in (1, 2):
        try:
            r = subprocess.run(list(spcmd) + ["--help"], capture_output=True,
                               text=True, errors="replace", timeout=180)
        except subprocess.TimeoutExpired:
            warn("engine --help probe timed out; continuing anyway")
            return
        except (FileNotFoundError, OSError) as e:
            die("cannot launch SpotiFLAC engine (%s): %s" % (" ".join(spcmd), e))
        if r.returncode == 0:
            if attempt == 2:
                info("Engine repaired successfully — continuing.")
            return
        err_txt = (r.stderr or "") + "\n" + (r.stdout or "")
        if attempt == 1 and "Non-UTF-8" in err_txt:
            m = re.search(r'File "([^"]+?[/\\]nodriver[/\\][^"]+?\.py)"', err_txt)
            if m:
                pkg_root = m.group(1)[:m.group(1).rindex("nodriver")] + "nodriver"
                warn("engine dependency is corrupted (non-UTF-8 bytes in %s) — repairing…" % pkg_root)
                fixed = _fix_bad_utf8_tree(pkg_root)
                warn("re-encoded %d file(s); retrying engine check" % len(fixed))
                continue
        tail = "\n  ".join(err_txt.strip().splitlines()[-12:])
        die("SpotiFLAC engine failed its startup check (command: %s):\n  %s"
            % (" ".join(spcmd), tail))


def _flac_state(outdir):
    """Map of every .flac under outdir -> mtime (for before/after comparison)."""
    state = {}
    for root, _dirs, files in os.walk(outdir):
        for f in files:
            if f.lower().endswith(".flac"):
                p = os.path.join(root, f)
                try:
                    state[p] = os.path.getmtime(p)
                except OSError:
                    pass
    return state


def _changed_flacs(before, after):
    """Paths of .flac files that are new or rewritten between two snapshots."""
    return [p for p, mt in after.items() if p not in before or mt > before[p] + 1e-6]


AUDIO_EXTS = (".flac", ".m4a", ".opus", ".mp3", ".ogg", ".webm", ".aac", ".wav")


# ---------------------------------------------------------------- cover art
def http_get_bytes(url, timeout=30):
    if not url:
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception:
        return None


def _mutagen():
    try:
        import mutagen
        return mutagen
    except ImportError:
        return None


def _img_dims(data):
    """(width, height) parsed from raw PNG/JPEG bytes, or (0, 0)."""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return (int.from_bytes(data[16:20], "big"),
                    int.from_bytes(data[20:24], "big"))
        if data[:3] == b"\xff\xd8\xff":              # JPEG: walk to an SOF marker
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    break
                m = data[i + 1]
                if 0xC0 <= m <= 0xCF and m not in (0xC4, 0xC8, 0xCC):
                    return (int.from_bytes(data[i + 7:i + 9], "big"),
                            int.from_bytes(data[i + 5:i + 7], "big"))
                i += 2 + int.from_bytes(data[i + 2:i + 4], "big")
    except Exception:
        pass
    return 0, 0


def repair_picture_dims(path):
    """Fix pictures embedded with 0x0 declared dimensions (the SpotiFLAC
    engine's tagger omits them, and picky players — VLC included — can refuse
    to show such art). Returns True if the file was rewritten."""
    mut = _mutagen()
    if not mut:
        return False
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".flac":
            from mutagen.flac import FLAC
            f = FLAC(path)
            changed = False
            pics = []
            for pic in f.pictures:
                if (not pic.width or not pic.height) and pic.data:
                    w, h = _img_dims(pic.data)
                    if w and h:
                        pic.width, pic.height = w, h
                        pic.depth = pic.depth or 24
                        changed = True
                pics.append(pic)
            if changed:
                f.clear_pictures()
                for pic in pics:
                    f.add_picture(pic)
                f.save()
            return changed
        if ext in (".opus", ".ogg"):
            from mutagen.flac import Picture
            f = mut.File(path)
            if f is None:
                return False
            blocks = f.get("metadata_block_picture", [])
            changed = False
            out = []
            for b64 in blocks:
                try:
                    pic = Picture(base64.b64decode(b64))
                    if (not pic.width or not pic.height) and pic.data:
                        w, h = _img_dims(pic.data)
                        if w and h:
                            pic.width, pic.height = w, h
                            pic.depth = pic.depth or 24
                            changed = True
                    out.append(base64.b64encode(pic.write()).decode("ascii"))
                except Exception:
                    out.append(b64)
            if changed:
                f["metadata_block_picture"] = out
                f.save()
            return changed
    except Exception:
        return False
    return False


def has_cover(path):
    """True if the file already carries embedded art (or we can't tell —
    uncertainty means leave the file alone)."""
    ext = os.path.splitext(path)[1].lower()
    mut = _mutagen()
    if mut:
        try:
            f = mut.File(path)
            if f is None:
                return True
            from mutagen.flac import FLAC
            from mutagen.mp4 import MP4
            if isinstance(f, FLAC):
                return bool(f.pictures)
            if isinstance(f, MP4):
                return bool(f.tags and f.tags.get("covr"))
            tags = getattr(f, "tags", None)
            if tags is None:
                return False
            try:
                keys = list(tags.keys())
            except Exception:
                return True
            return any(k.lower().startswith(("apic", "metadata_block_picture", "covr"))
                       for k in keys)
        except Exception:
            return True
    if which("ffprobe"):
        try:
            r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v",
                                "-show_entries", "stream=codec_name", "-of", "csv=p=0", path],
                               capture_output=True, text=True, timeout=60)
            return bool(r.stdout.strip())
        except Exception:
            return True
    return True


def _square_crop(img_bytes):
    """Center-crop an image to square via ffmpeg (for 16:9 YouTube thumbs)."""
    if not which("ffmpeg"):
        return img_bytes
    import tempfile as _tf
    try:
        with _tf.NamedTemporaryFile(suffix=".jpg", delete=False) as fin:
            fin.write(img_bytes)
        fout = fin.name + ".sq.jpg"
        r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", fin.name,
                            "-vf", "crop='min(iw,ih)':'min(iw,ih)'", "-q:v", "2", fout],
                           capture_output=True, timeout=60)
        if r.returncode == 0 and os.path.getsize(fout) > 500:
            img_bytes = open(fout, "rb").read()
        for p in (fin.name, fout):
            try:
                os.unlink(p)
            except OSError:
                pass
    except Exception:
        pass
    return img_bytes


def fetch_art(art_url, ytid):
    """Best available cover image: the match's album art, else the YouTube
    thumbnail square-cropped. Returns JPEG/PNG bytes or None."""
    data = http_get_bytes(art_url)
    if data and len(data) > 1000:
        return data
    if ytid:
        for name in ("maxresdefault", "hqdefault", "mqdefault"):
            data = http_get_bytes("https://i.ytimg.com/vi/%s/%s.jpg" % (ytid, name))
            if data and len(data) > 2000:
                return _square_crop(data)
    return None


def embed_cover(path, img):
    """Embed art into flac/m4a/opus/ogg/mp3. mutagen preferred; ffmpeg
    rewrite as fallback for flac/mp3/m4a. Returns True on success."""
    ext = os.path.splitext(path)[1].lower()
    mut = _mutagen()
    if mut:
        try:
            mime = "image/png" if img[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
            if ext == ".flac":
                from mutagen.flac import FLAC, Picture
                f = FLAC(path)
                pic = Picture()
                pic.type = 3
                pic.mime = mime
                pic.desc = "Cover"
                pic.data = img
                pic.width, pic.height = _img_dims(img)
                pic.depth = 24
                f.clear_pictures()
                f.add_picture(pic)
                f.save()
                return True
            if ext in (".m4a", ".mp4", ".aac"):
                from mutagen.mp4 import MP4, MP4Cover
                f = MP4(path)
                fmt = MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
                f["covr"] = [MP4Cover(img, imageformat=fmt)]
                f.save()
                return True
            if ext in (".opus", ".ogg"):
                from mutagen.flac import Picture
                pic = Picture()
                pic.type = 3
                pic.mime = mime
                pic.desc = "Cover"
                pic.data = img
                pic.width, pic.height = _img_dims(img)
                pic.depth = 24
                f = mut.File(path)
                if f is None:
                    return False
                f["metadata_block_picture"] = [base64.b64encode(pic.write()).decode("ascii")]
                f.save()
                return True
            if ext == ".mp3":
                from mutagen.id3 import ID3, APIC, ID3NoHeaderError
                try:
                    tags = ID3(path)
                except ID3NoHeaderError:
                    tags = ID3()
                tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=img))
                tags.save(path)
                return True
        except Exception as e:
            warn("mutagen embed failed for %s (%s) — trying ffmpeg" % (os.path.basename(path), e))
    if which("ffmpeg") and ext in (".flac", ".mp3", ".m4a"):
        import tempfile as _tf
        try:
            with _tf.NamedTemporaryFile(suffix=".jpg", delete=False) as fi:
                fi.write(img)
            tmp = path + ".cover.tmp" + ext
            r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", path, "-i", fi.name,
                                "-map", "0:a", "-map", "1:v", "-c", "copy",
                                "-disposition:v", "attached_pic",
                                "-metadata:s:v", "comment=Cover (front)", tmp],
                               capture_output=True, timeout=180)
            os.unlink(fi.name)
            if r.returncode == 0 and os.path.getsize(tmp) > os.path.getsize(path) // 2:
                os.replace(tmp, path)
                return True
            try:
                os.unlink(tmp)
            except OSError:
                pass
        except Exception:
            pass
    return False


def ensure_cover(files, art_url, ytid):
    """Guarantee embedded art on every delivered file — and guarantee the art
    is written in a way players will actually display (the engine embeds
    pictures with declared dimensions 0x0, which VLC and other picky players
    refuse to show; repair that immediately, per track). Never raises, never
    fails the track — art problems are warnings only."""
    art = None
    for p in files:
        try:
            if os.path.splitext(p)[1].lower() not in AUDIO_EXTS:
                continue
            if has_cover(p):
                if repair_picture_dims(p):
                    good("art metadata repaired (0x0 dims): %s" % os.path.basename(p))
                continue
            if art is None:
                art = fetch_art(art_url, ytid) or False
            if not art:
                warn("no cover art source found for %s" % os.path.basename(p))
                return
            if embed_cover(p, art):
                good("cover art embedded: %s" % os.path.basename(p))
            else:
                warn("could not embed cover art in %s" % os.path.basename(p))
        except Exception as e:
            warn("cover art step failed for %s (%s)" % (os.path.basename(p), e))


def _norm_stem(s):
    """Normalized comparison form of a filename stem / track name: unify quotes,
    drop quote chars entirely (the engine strips them when naming files),
    lowercase, collapse whitespace."""
    s = _fix_quotes(s or "")
    s = s.replace('"', "").replace("'", "")
    return re.sub(r"\s+", " ", s).strip().lower()


def _find_track_files(outdir, names):
    """Files on disk that plausibly belong to a track with one of these names.
    A stem matches only exactly or as 'Name - <artists>' — so the track 'Rang'
    does NOT match the file 'Rang Jo Lagyo - …'. Returns relative paths."""
    wanted = [_norm_stem(n) for n in names if n and _norm_stem(n)]
    if not wanted:
        return []
    hits = []
    for root, _dirs, files in os.walk(outdir):
        for f in files:
            if not f.lower().endswith(AUDIO_EXTS):
                continue
            stem = _norm_stem(os.path.splitext(f)[0])
            for n in wanted:
                if stem == n or stem.startswith(n + " - "):
                    hits.append(os.path.relpath(os.path.join(root, f), outdir))
                    break
    return hits


def _audio_state(outdir):
    """Like _flac_state but for any audio file (used by the YouTube fallback)."""
    state = {}
    for root, _dirs, files in os.walk(outdir):
        for f in files:
            if f.lower().endswith(AUDIO_EXTS):
                p = os.path.join(root, f)
                try:
                    state[p] = os.path.getmtime(p)
                except OSError:
                    pass
    return state


def _valid_audio(path):
    """Decode-verify any audio file with ffmpeg (True if ffmpeg is absent)."""
    if not which("ffmpeg"):
        return True
    try:
        r = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-f", "null", "-"],
                           capture_output=True, timeout=120)
        return r.returncode == 0
    except Exception:
        return True


def yt_fallback(entry, outdir, timeout, to_flac):
    """Last resort when no lossless source exists: download the YouTube video's
    own audio at best quality. Native container by default (.m4a/.opus — so
    lossy fallbacks are distinguishable from true FLAC at a glance); to_flac
    wraps it in a FLAC container instead. Success is judged by a validated
    audio file on disk, NOT the exit code (a failed thumbnail embed must not
    discard a good download)."""
    ytdlp = find_ytdlp()
    if not ytdlp:
        warn("yt-dlp not found — cannot fall back to YouTube audio")
        return "fail", []
    cmd = [ytdlp, "-f", "bestaudio/best", "--no-playlist", "--no-progress",
           "-o", os.path.join(outdir, "%(title)s.%(ext)s")]
    if which("ffmpeg"):
        cmd += ["-x", "--embed-metadata", "--embed-thumbnail"]
        if to_flac:
            cmd += ["--audio-format", "flac"]
    cmd.append(entry["url"])
    before = _audio_state(outdir)
    try:
        proc = subprocess.Popen(cmd, start_new_session=True,
                                stdin=subprocess.DEVNULL)
    except (FileNotFoundError, OSError) as e:
        warn("cannot launch yt-dlp (%s)" % e)
        return "fail", []
    try:
        proc.wait(timeout=timeout if timeout and timeout > 0 else None)
    except subprocess.TimeoutExpired:
        warn("YouTube fallback timed out after %ss — killing it" % timeout)
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            proc.kill()
        try:
            proc.wait(timeout=10)
        except Exception:
            pass
        return "timeout", []
    changed = [p for p, mt in _audio_state(outdir).items()
               if p not in before or mt > before[p] + 1e-6]
    valid = [p for p in changed if _valid_audio(p)]
    for p in valid:
        good("saved (YouTube audio): %s" % os.path.relpath(p, outdir))
    return ("ok", valid) if valid else ("fail", [])


def _valid_flac(path):
    """Integrity check: real FLAC header + (if ffmpeg is present) a full decode.

    Catches the failure mode where a service writes an error page / encrypted
    blob / truncated stream to a .flac filename — seen in the wild with the
    Deezer path — which would otherwise be counted as a successful download."""
    try:
        with open(path, "rb") as f:
            if f.read(4) != b"fLaC":
                return False
    except OSError:
        return False
    if which("ffmpeg"):
        try:
            r = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-f", "null", "-"],
                               capture_output=True, timeout=120)
            return r.returncode == 0
        except Exception:
            return True   # ffmpeg hiccup: fall back to trusting the header
    return True


def run_spotiflac(cmd_base, spotify_url, outdir, services, fmt, extra, timeout):
    """Run SpotiFLAC for one track and VERIFY the result by looking for a new
    .flac on disk — SpotiFLAC can exit 0 even when every service failed, so the
    exit code alone can't be trusted. Bounded by `timeout` seconds (0 = no
    limit) so a hung service can't freeze the batch.

    Returns: (status, valid_files) with status "ok"|"no-file"|"bad-file"|"fail"|"timeout"
    """
    cmd = list(cmd_base) + [spotify_url, outdir, "--service"] + services + \
        ["--filename-format", fmt] + list(extra)
    before = _flac_state(outdir)
    try:
        # stdin closed: some engine extensions stop to ask for manual browser
        # verification tokens — in a batch that must fail fast, never block.
        proc = subprocess.Popen(cmd, start_new_session=True,
                                stdin=subprocess.DEVNULL)
    except FileNotFoundError as e:
        warn("cannot launch SpotiFLAC (%s)" % e)
        return "fail", []
    try:
        rc = proc.wait(timeout=timeout if timeout and timeout > 0 else None)
    except subprocess.TimeoutExpired:
        warn("track timed out after %ss — a service hung; killing it" % timeout)
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            proc.kill()
        try:
            proc.wait(timeout=10)
        except Exception:
            pass
        return "timeout", []
    if rc != 0:
        return "fail", []
    changed = _changed_flacs(before, _flac_state(outdir))
    if not changed:
        return "no-file", []
    valid = [p for p in changed if _valid_flac(p)]
    for p in changed:
        if p not in valid:
            warn("corrupt/invalid FLAC written (kept for inspection): %s" % p)
    if not valid:
        return "bad-file", []
    for p in valid:
        good("saved: %s" % os.path.relpath(p, outdir))
    return "ok", valid


# ---------------------------------------------------------------- manifest (resume)
def load_manifest(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def save_manifest(path, data):
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        warn("could not write manifest: %s" % e)


# ---------------------------------------------------------------- main
def build_parser():
    p = argparse.ArgumentParser(
        prog="ytflac",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Download true FLAC (via SpotiFLAC) for the songs in a YouTube "
                    "song or playlist URL.",
        epilog="examples:\n"
               "  python3 ytflac.py \"https://youtu.be/dQw4w9WgXcQ\"\n"
               "  python3 ytflac.py \"https://www.youtube.com/playlist?list=PL...\"\n"
               "  python3 ytflac.py --dry-run --service qobuz tidal \"https://youtu.be/...\"\n")
    p.add_argument("urls", nargs="*", metavar="URL",
                   help="YouTube song or playlist URL(s) (optional with --fix-covers)")
    p.add_argument("-o", "--output", default=os.path.expanduser("~/Music/YouTube-FLAC"),
                   help="output directory (default: ~/Music/YouTube-FLAC)")
    p.add_argument("--service", nargs="+", default=["qobuz", "deezer", "tidal", "amazon"],
                   choices=["tidal", "amazon", "qobuz", "deezer"],
                   help="lossless services SpotiFLAC tries, in order "
                        "(default: qobuz deezer tidal amazon — Amazon last as it can hang)")
    p.add_argument("--filename-format", default="{title} - {artist}",
                   help='SpotiFLAC filename template (default: "{title} - {artist}")')
    p.add_argument("--resolver", choices=["auto", "odesli", "spotify"], default="auto",
                   help="match YouTube->Spotify: 'auto' (default) = fast Spotify search, "
                        "Odesli only as fallback; 'spotify' = Spotify search only (fastest); "
                        "'odesli' = exact-by-URL only (precise but heavily rate-limited)")
    p.add_argument("--spotiflac", default=None,
                   help="command to run SpotiFLAC (else env SPOTIFLAC or auto-detect)")
    p.add_argument("--yes-playlist", action="store_true",
                   help="if a watch URL is part of a playlist, grab the whole playlist")
    p.add_argument("--limit", type=int, default=0, help="only the first N tracks of a playlist")
    p.add_argument("--sleep", type=float, default=6.5,
                   help="max seconds to pace Odesli fallback calls (default 6.5; unused when "
                        "Spotify search already matches)")
    p.add_argument("--track-timeout", type=int, default=300,
                   help="seconds before giving up on a single track's download "
                        "(default 300; 0 = no limit). Guards against a hung service.")
    p.add_argument("--redo", action="store_true",
                   help="re-attempt tracks already marked done in the manifest "
                        "(also retries YouTube-audio fallbacks as lossless)")
    p.add_argument("--no-fallback", action="store_true",
                   help="purist mode: skip tracks with no lossless source instead of "
                        "downloading the YouTube audio")
    p.add_argument("--fallback-flac", action="store_true",
                   help="store YouTube-audio fallbacks in a FLAC container instead of "
                        "their native .m4a/.opus (note: still lossy inside)")
    p.add_argument("--fix-covers", action="store_true",
                   help="repair mode: scan the output folder and embed cover art into "
                        "any audio file that lacks it (no URL needed)")
    p.add_argument("--check-covers", action="store_true",
                   help="report mode: list which files have embedded art and which "
                        "don't — changes nothing (no URL needed)")
    p.add_argument("--dry-run", action="store_true",
                   help="resolve matches and print them; do not download")
    return p


def resolve(entry, resolver, spotify, sleep_base, ctx):
    """Return a match dict {url,name,artist,isrc,via} or None.

    'auto' tries Spotify search first (fast, generous limits, and — with the
    title parser — accurate), and only falls back to Odesli for the few tracks
    Spotify can't place. That keeps big playlists fast and rarely touches
    Odesli's rate limit."""
    art_hint, trk_hint, alb_hint = parse_track(entry["title"], entry["uploader"])
    if entry.get("artist"):
        art_hint = entry["artist"]
    if entry.get("track"):
        trk_hint = entry["track"]
    if entry.get("album"):
        alb_hint = entry["album"]

    order = {"auto": ["spotify", "odesli"],
             "odesli": ["odesli"],
             "spotify": ["spotify"]}[resolver]
    for how in order:
        if how == "spotify":
            m = spotify.search(art_hint, trk_hint, alb_hint, entry.get("duration") or 0)
            if m:
                m["via"] = "spotify-search"
                return m
        else:
            if ctx.get("odesli_off"):
                continue
            m = odesli(entry["url"], ctx)
            if m:
                m["via"] = "odesli"
                return m
            if not ctx.get("odesli_off"):
                time.sleep(min(sleep_base, 3))  # gentle pacing, only when Odesli is active
    return None


def check_covers(outdir):
    """Read-only report: which files carry embedded art, which don't."""
    if not os.path.isdir(outdir):
        die("no such folder: %s" % outdir)
    total = with_art = 0
    bare = []
    for root, _dirs, files in os.walk(outdir):
        for fname in sorted(files):
            if not fname.lower().endswith(AUDIO_EXTS):
                continue
            total += 1
            p = os.path.join(root, fname)
            try:
                if has_cover(p):
                    with_art += 1
                else:
                    bare.append(os.path.relpath(p, outdir))
            except Exception:
                bare.append(os.path.relpath(p, outdir) + "  (unreadable)")
    for b in bare:
        warn("MISSING art: %s" % b)
    info("Cover check: %d audio files — %d have embedded art, %d missing."
         % (total, with_art, len(bare)))
    if bare:
        info("Run with --fix-covers to repair the missing ones.")
    else:
        info("Every file carries art. If you don't see it, the viewer is the issue: "
             "macOS Finder can't show embedded art for FLAC/Opus — check in VLC, IINA, "
             "or another real player.")
    return 0


def fix_covers(outdir):
    """Walk the library and embed cover art into every audio file missing it.
    Art comes from a Spotify lookup of the filename ('{title} - {artist}' first,
    then swapped, then title alone). Files we can't match are reported."""
    if not os.path.isdir(outdir):
        die("no such folder: %s" % outdir)
    spotify = Spotify()
    total = fixed = already = repaired = failed = 0
    for root, _dirs, files in os.walk(outdir):
        for fname in sorted(files):
            if not fname.lower().endswith(AUDIO_EXTS):
                continue
            p = os.path.join(root, fname)
            total += 1
            try:
                if has_cover(p):
                    if repair_picture_dims(p):
                        good("art metadata repaired (declared size was 0x0): %s" % fname)
                        repaired += 1
                    else:
                        already += 1
                    continue
                stem = os.path.splitext(fname)[0]
                art_url = None
                if " - " in stem:
                    left, right = stem.split(" - ", 1)
                    tries = [(right.strip(), left.strip()),   # our "{title} - {artist}"
                             (left.strip(), right.strip())]   # "Artist - Title" uploads
                else:
                    _a, _t, _alb = parse_track(stem, "")
                    tries = [(_a, _t)]
                for artist, title in tries:
                    m = spotify.search(artist, title, "", 0)
                    if m and m.get("art"):
                        art_url = m["art"]
                        break
                art = fetch_art(art_url, None)
                if not art:
                    warn("no art found for: %s" % fname)
                    failed += 1
                    continue
                if embed_cover(p, art):
                    good("cover art embedded: %s" % fname)
                    fixed += 1
                else:
                    warn("could not embed art in: %s" % fname)
                    failed += 1
            except KeyboardInterrupt:
                raise
            except Exception as e:
                warn("error on %s (%s) — continuing" % (fname, e))
                failed += 1
    print()
    info("Cover art repair: %d files scanned, %d already fine, %d metadata-repaired, "
         "%d art added, %d not fixable." % (total, already, repaired, fixed, failed))
    return 0


def main(argv):
    # split trailing "-- <extra spotiflac args>"
    extra = []
    if "--" in argv:
        i = argv.index("--")
        extra = argv[i + 1:]
        argv = argv[:i]
    args = build_parser().parse_args(argv)

    outdir = os.path.abspath(os.path.expanduser(args.output))

    # ---- report mode: which files have embedded art? (read-only) ----
    if args.check_covers:
        return check_covers(outdir)
    # ---- repair mode: add missing cover art to the existing library ----
    # (alone: repair only; combined with URLs: download first, then repair)
    if args.fix_covers and not args.urls:
        return fix_covers(outdir)
    if not args.urls:
        build_parser().error("no URL given (or use --fix-covers / --check-covers)")

    spcmd = find_spotiflac(args.spotiflac)
    if not spcmd and not args.dry_run:
        die("SpotiFLAC not found. Install the maintained engine:\n"
            "  pip3 install --break-system-packages \\\n"
            "      \"git+https://github.com/ShuShuzinhuu/SpotiFLAC-Module-Version\" nodriver\n"
            "  export SPOTIFLAC=\"python3 -m SpotiFLAC\"\n"
            "…then re-run (or pass --spotiflac).")

    if not args.dry_run:
        os.makedirs(outdir, exist_ok=True)
        preflight_spotiflac(spcmd)   # fail fast (and self-repair) before any track work
    manifest_path = os.path.join(outdir, ".ytflac-manifest.json")
    manifest = load_manifest(manifest_path) if not args.dry_run else {}
    spotify = Spotify()

    # 1) enumerate every YouTube track across all given URLs
    tracks = []
    for url in args.urls:
        info("Reading YouTube: %s" % url)
        ents, plname = yt_entries(url, args.yes_playlist, args.limit)
        if plname:
            info("Playlist '%s' — %d tracks" % (plname, len(ents)))
        elif len(ents) == 1:
            info("Single track")
        tracks.extend(ents)
    if not tracks:
        die("no playable tracks found in the given URL(s)")

    info("Matching %d track(s) to Spotify, then handing to SpotiFLAC…" % len(tracks))
    done = ytcnt = skipped = failed = 0
    ctx = {}  # shared run state (e.g. Odesli rate-limit circuit breaker)

    def _finish(track_id, status, **extra_fields):
        rec = {"status": status}
        rec.update(extra_fields)
        manifest[track_id] = rec
        if not args.dry_run:
            save_manifest(manifest_path, manifest)

    for idx, e in enumerate(tracks, 1):
        tag = "[%d/%d] %s" % (idx, len(tracks), e["title"][:70])
        try:
            rec = manifest.get(e["id"])
            if rec and rec.get("status") in ("done", "yt-fallback") and not args.redo:
                recorded = rec.get("files") or []
                if recorded:
                    if any(os.path.exists(os.path.join(outdir, f)) for f in recorded):
                        good("%s — already downloaded, skipping" % tag)
                        skipped += 1
                        continue
                    info("%s — file was deleted; re-downloading" % tag)
                else:
                    # legacy manifest entry (no file list recorded): look for a
                    # plausible file on disk before trusting the 'done' status
                    names = [rec.get("title", "")]
                    matched = rec.get("matched", "")
                    if " — " in matched:
                        names.append(matched.split(" — ", 1)[1])
                    names.append(parse_track(rec.get("title", ""), "")[1])
                    found = _find_track_files(outdir, names)
                    if found:
                        good("%s — already downloaded, skipping" % tag)
                        rec["files"] = found
                        manifest[e["id"]] = rec
                        save_manifest(manifest_path, manifest)
                        skipped += 1
                        continue
                    info("%s — marked done but no file on disk; re-downloading" % tag)

            m = resolve(e, args.resolver, spotify, args.sleep, ctx)

            # ---- no lossless match anywhere -> YouTube best-audio fallback ----
            if not m:
                if args.no_fallback:
                    warn("%s — no Spotify match; skipped (--no-fallback)" % tag)
                    failed += 1
                    _finish(e["id"], "unmatched", title=e["title"])
                    continue
                if args.dry_run:
                    print("  %s\n      -> no lossless match; will download YouTube best audio" % tag)
                    ytcnt += 1
                    continue
                warn("%s — no lossless match; downloading the YouTube audio instead" % tag)
                fres, ffiles = yt_fallback(e, outdir, args.track_timeout, args.fallback_flac)
                if fres == "ok":
                    ensure_cover(ffiles, None, e["id"])
                    ytcnt += 1
                    _finish(e["id"], "yt-fallback", title=e["title"], source="youtube",
                            files=[os.path.relpath(p, outdir) for p in ffiles])
                else:
                    failed += 1
                    warn("YouTube fallback failed (%s) for: %s — will retry next run" % (fres, tag))
                    _finish(e["id"], "error", title=e["title"])
                continue

            label = "%s — %s" % (m.get("artist") or "?", m.get("name") or "?")
            if args.dry_run:
                print("  %s\n      -> %s  (%s)\n      %s" %
                      (tag, label, m["via"], m["url"]))
                done += 1
                continue

            info("%s\n      -> %s  via %s" % (tag, label, m["via"]))
            res, files = run_spotiflac(spcmd, m["url"], outdir, args.service,
                                       args.filename_format, extra, args.track_timeout)
            if res == "ok":
                ensure_cover(files, m.get("art"), e["id"])
                done += 1
                _finish(e["id"], "done", spotify=m["url"], title=e["title"],
                        matched=label, via=m["via"],
                        files=[os.path.relpath(p, outdir) for p in files])
                continue

            # ---- lossless download failed -> YouTube best-audio fallback ----
            if res == "no-file":
                existing = _find_track_files(outdir, [m.get("name"), e["title"]])
                if existing:
                    good("file already on disk for %s — marking done" % label)
                    ensure_cover([os.path.join(outdir, f) for f in existing],
                                 m.get("art"), e["id"])
                    done += 1
                    _finish(e["id"], "done", spotify=m["url"], title=e["title"],
                            matched=label, via=m["via"], files=existing)
                    continue
                warn("no new FLAC appeared for: %s (services failed)" % label)
            elif res == "bad-file":
                warn("downloaded file failed FLAC integrity check for: %s" % label)
            else:
                warn("SpotiFLAC failed (%s) for: %s" % (res, label))
            if args.no_fallback:
                failed += 1
                _finish(e["id"], res, spotify=m["url"], title=e["title"], matched=label)
                continue
            warn("falling back to YouTube audio for: %s" % label)
            fres, ffiles = yt_fallback(e, outdir, args.track_timeout, args.fallback_flac)
            if fres == "ok":
                ensure_cover(ffiles, m.get("art"), e["id"])
                ytcnt += 1
                _finish(e["id"], "yt-fallback", spotify=m["url"], title=e["title"],
                        matched=label, source="youtube",
                        files=[os.path.relpath(p, outdir) for p in ffiles])
            else:
                failed += 1
                warn("YouTube fallback also failed (%s) — will retry next run" % fres)
                _finish(e["id"], "error", spotify=m["url"], title=e["title"], matched=label)

        except KeyboardInterrupt:
            raise
        except Exception as ex:
            # crash shield: no single track may stop the batch, for any reason
            failed += 1
            warn("unexpected error on %s (%s: %s) — continuing with next track"
                 % (tag, type(ex).__name__, ex))
            try:
                _finish(e["id"], "error", title=e.get("title", ""))
            except Exception:
                pass

    print()
    if args.dry_run:
        info("Dry run: %d lossless-matched, %d will use YouTube audio, %d skipped "
             "(nothing downloaded)." % (done, ytcnt, failed))
    else:
        info("Finished: %d lossless, %d YouTube-audio, %d skipped, %d failed. Files in: %s"
             % (done, ytcnt, skipped, failed, outdir))
        if args.fix_covers:
            print()
            info("Now sweeping the whole folder for missing cover art…")
            fix_covers(outdir)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        die("interrupted", 130)
