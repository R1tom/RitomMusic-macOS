#!/usr/bin/env python3
"""
ipod_sync — put music of any format onto a click-wheel iPod, from the command line.

  ipod_sync.py add <files-or-folders> [--format aac] [--bitrate 256] [--playlist NAME]
  ipod_sync.py list [--search TEXT]
  ipod_sync.py remove --search TEXT [--yes]
  ipod_sync.py rebuild
  ipod_sync.py info

Needs: python3 and ffmpeg. On macOS it uses Apple's own AAC encoder when available.
Everything it knows lives in <iPod>/.ipod_sync/catalog.json, so the library travels
with the device. The existing library is imported automatically on first run.
"""
import argparse, glob, json, os, random, re, shutil, subprocess, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ipod_db

AUDIO_EXT = {'.flac','.opus','.m4a','.mp3','.wav','.aiff','.aif','.ogg','.wma','.alac',
             '.aac','.mp4','.wv','.ape','.mpc','.oga','.webm','.mka','.dsf','.aifc'}
FORMATS = {
    'aac':  dict(ext='.m4a', filetype='AAC audio file',            unk126=0xFFFF, unk144=0x33),
    'alac': dict(ext='.m4a', filetype='Apple Lossless audio file', unk126=0xFFFF, unk144=0x33),
    'aiff': dict(ext='.aif', filetype='AIFF audio file',           unk126=0x0000, unk144=0x00),
}

def log(*a):
    try: print(*a, flush=True)
    except BrokenPipeError:
        try: sys.stdout.close()
        except Exception: pass
        os._exit(0)

# ---------- device ------------------------------------------------------------
def find_volume(explicit=None):
    if explicit:
        return explicit if os.path.isdir(os.path.join(explicit,'iPod_Control','Music')) else None
    env = os.environ.get('IPOD_VOL')
    if env and os.path.isdir(os.path.join(env,'iPod_Control','Music')):
        return env
    roots = sorted(glob.glob('/Volumes/*')) or sorted(glob.glob('/media/*/*'))
    for d in roots:
        if os.path.isdir(os.path.join(d,'iPod_Control','Music')):
            return d
    return None

def tool(name, fallbacks=()):
    p = shutil.which(name)
    if p: return p
    for c in fallbacks:
        if os.path.isfile(c) and os.access(c, os.X_OK): return c
    return None

FFMPEG  = tool('ffmpeg',  ['/usr/local/bin/ffmpeg','/opt/homebrew/bin/ffmpeg'])
FFPROBE = tool('ffprobe', ['/usr/local/bin/ffprobe','/opt/homebrew/bin/ffprobe'])
AFCONVERT = tool('afconvert')

def run(cmd, timeout=1800):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

_ENC = None
def encoder():
    global _ENC
    if _ENC is None:
        out = run([FFMPEG,'-hide_banner','-encoders']).stdout if FFMPEG else ''
        _ENC = 'aac_at' if 'aac_at' in out else ('afconvert' if AFCONVERT else 'native')
    return _ENC

# ---------- catalog -----------------------------------------------------------
CATALOG_META = {}

def catalog_path(vol): return os.path.join(vol, '.ipod_sync', 'catalog.json')

def load_catalog(vol):
    """Returns (tracks, playlists). playlists = [{'name':str, 'dbids':[int, ...]}]"""
    p = catalog_path(vol)
    if os.path.exists(p):
        with open(p) as f: data = json.load(f)
        if isinstance(data, list):                  # catalog written before playlists existed
            return data, []
        CATALOG_META.update(data.get('meta', {}))
        return data.get('tracks', []), data.get('playlists', [])
    tracks, playlists = [], []
    db = os.path.join(vol,'iPod_Control','iTunes','iTunesDB')
    if os.path.exists(db):
        with open(db,'rb') as f: raw = f.read()
        tracks = ipod_db.read_itunesdb(raw)
        for t in tracks:
            t.setdefault('gapless', True)
            t['source'] = t.get('source','(already on iPod)')
        nm = ipod_db.read_device_name(raw)
        if nm: CATALOG_META['device_name'] = nm
        by_id = {t.get('track_id'): t['dbid'] for t in tracks}
        found = list(ipod_db.read_playlists(raw))
        found += ipod_db.read_otg_playlists(os.path.join(vol,'iPod_Control','iTunes'))
        for name, ids in found:
            dbids = [by_id[i] for i in ids if i in by_id]
            if dbids and not any(p['name'] == name for p in playlists):
                playlists.append({'name': name, 'dbids': dbids})
        log(f'imported {len(tracks)} existing tracks'
            + (f' and {len(playlists)} playlist(s)' if playlists else '')
            + ' from the iPod database')
    return tracks, playlists

def save_catalog(vol, tracks, playlists):
    d = os.path.join(vol,'.ipod_sync'); os.makedirs(d, exist_ok=True)
    tmp = catalog_path(vol) + '.tmp'
    with open(tmp,'w') as f:
        json.dump({'version': 2, 'meta': CATALOG_META,
                   'tracks': tracks, 'playlists': playlists},
                  f, indent=1, ensure_ascii=False)
    os.replace(tmp, catalog_path(vol))

def backup_db(vol):
    src = os.path.join(vol,'iPod_Control','iTunes','iTunesDB')
    if not os.path.exists(src): return
    d = os.path.join(vol,'.ipod_sync','backups'); os.makedirs(d, exist_ok=True)
    dst = os.path.join(d, 'iTunesDB.%s' % time.strftime('%Y%m%d-%H%M%S'))
    shutil.copy2(src, dst)
    keep = sorted(glob.glob(os.path.join(d,'iTunesDB.*')))[-5:]
    for old in glob.glob(os.path.join(d,'iTunesDB.*')):
        if old not in keep:
            try: os.remove(old)
            except OSError: pass

# ---------- probing / conversion ----------------------------------------------
def probe(path):
    r = run([FFPROBE,'-v','quiet','-print_format','json','-show_format','-show_streams',path], 120)
    try: d = json.loads(r.stdout)
    except Exception: return None
    aud = [s for s in d.get('streams',[]) if s.get('codec_type')=='audio']
    if not aud: return None
    a = aud[0]; fmt = d.get('format',{})
    tags = {}
    tags.update({k.lower():v for k,v in (fmt.get('tags') or {}).items()})
    tags.update({k.lower():v for k,v in (a.get('tags') or {}).items()})
    def tag(*keys):
        for k in keys:
            if tags.get(k): return str(tags[k]).strip()
        return ''
    def num(v):
        v = str(v).split('/')[0]
        return int(v) if v.isdigit() else 0
    year = 0
    for src in (tag('date'), tag('year'), tag('originalyear')):
        m = re.search(r'(19|20)\d{2}', src)
        if m: year = int(m.group(0)); break
    stem = os.path.splitext(os.path.basename(path))[0]
    title, artist = tag('title'), tag('artist','album_artist')
    if not title or not artist:
        parts = stem.rsplit(' - ', 1)
        title = title or parts[0].strip()
        if not artist and len(parts) > 1: artist = parts[1].strip()
    return dict(codec=a.get('codec_name'), samplerate=int(a.get('sample_rate') or 44100),
                duration=float(fmt.get('duration') or a.get('duration') or 0),
                bits=str(a.get('bits_per_raw_sample') or ''), has_art=any(
                    s.get('codec_type')=='video' for s in d.get('streams',[])),
                title=title, artist=artist, album=tag('album'),
                albumartist=tag('album_artist','albumartist') or artist,
                genre=tag('genre'), year=year,
                track_nr=num(tag('track','tracknumber')), disc_nr=num(tag('disc','discnumber')))

def encode(src, dst, fmt, bitrate, info):
    """Convert one file. Returns (ok, message)."""
    tmp = dst + '.part'
    common = ['-map','0:a:0','-ar','44100','-vn']
    if fmt == 'aiff':
        cmd = [FFMPEG,'-y','-v','error','-i',src] + common + ['-c:a','pcm_s16be','-f','aiff',tmp]
    elif fmt == 'alac':
        cmd = [FFMPEG,'-y','-v','error','-i',src] + common + \
              ['-c:a','alac','-sample_fmt','s16p','-movflags','+faststart','-f','ipod',tmp]
    elif info['codec'] == 'aac':                       # already AAC: keep it as-is
        cmd = [FFMPEG,'-y','-v','error','-i',src,'-map','0:a:0','-c:a','copy',
               '-movflags','+faststart','-f','ipod',tmp]
    elif encoder() == 'aac_at':
        cmd = [FFMPEG,'-y','-v','error','-i',src] + common + \
              ['-c:a','aac_at','-b:a',str(bitrate*1000),'-movflags','+faststart','-f','ipod',tmp]
    elif encoder() == 'afconvert':
        wav, raw = tmp+'.wav', tmp+'.raw.m4a'
        r = run([FFMPEG,'-y','-v','error','-i',src,'-map','0:a:0','-c:a','pcm_s16le',
                 '-ar','44100','-ac','2','-f','wav',wav])
        if r.returncode: return False, 'decode failed'
        r = run([AFCONVERT,'-f','m4af','-d','aac','-b',str(bitrate*1000),'-q','127','-s','2',wav,raw])
        os.path.exists(wav) and os.remove(wav)
        if r.returncode: return False, 'afconvert failed'
        r = run([FFMPEG,'-y','-v','error','-i',raw,'-map','0:a:0','-c:a','copy',
                 '-movflags','+faststart','-f','ipod',tmp])
        os.path.exists(raw) and os.remove(raw)
        cmd = None
        if r.returncode: return False, 'remux failed'
    else:
        cmd = [FFMPEG,'-y','-v','error','-i',src] + common + \
              ['-c:a','aac','-b:a',str(bitrate*1000),'-movflags','+faststart','-f','ipod',tmp]
    if cmd:
        r = run(cmd)
        if r.returncode:
            os.path.exists(tmp) and os.remove(tmp)
            return False, (r.stderr or '').strip()[:120]
    os.replace(tmp, dst)
    return True, ''

def measure(path):
    r = run([FFPROBE,'-v','quiet','-print_format','json','-show_streams','-select_streams','a:0',path],120)
    st = json.loads(r.stdout)['streams'][0]
    sr = int(st['sample_rate'])
    num, den = (int(x) for x in st.get('time_base', '1/%d'%sr).split('/'))
    samples = int(round(int(st['duration_ts'])*num/den*sr)) if st.get('duration_ts') else 0
    return sr, samples, float(st.get('duration') or 0), st['codec_name']

# ---------- commands ----------------------------------------------------------
def gather(paths):
    out = []
    for p in paths:
        p = os.path.expanduser(p)
        if os.path.isdir(p):
            for root, _, files in os.walk(p):
                for f in sorted(files):
                    if os.path.splitext(f)[1].lower() in AUDIO_EXT and not f.startswith('._'):
                        out.append(os.path.join(root,f))
        elif os.path.isfile(p) and os.path.splitext(p)[1].lower() in AUDIO_EXT:
            out.append(p)
    return out

def next_index(tracks):
    used = set()
    for t in tracks:
        m = re.search(r'T(\d{4})\.', t.get('path',''))
        if m: used.add(int(m.group(1)))
    i = 0
    while i in used: i += 1
    return i, used

def matches(tracks, query, dbids=None):
    if dbids:                                   # exact selection (used by the Mac app)
        want = {int(x) for x in str(dbids).split(',') if x.strip()}
        return [t for t in tracks if t.get('dbid') in want]
    q = (query or '').lower()
    return [t for t in tracks
            if q in f"{t.get('artist','')} {t.get('title','')} {t.get('album','')} "
                    f"{t.get('albumartist','')} {t.get('genre','')}".lower()]

def find_playlist(playlists, name, create=False):
    for pl in playlists:
        if pl['name'].lower() == name.lower(): return pl
    if create:
        pl = {'name': name, 'dbids': []}
        playlists.append(pl)
        return pl
    return None

def cmd_add(vol, args):
    files = gather(args.paths)
    if not files: log('no audio files found'); return 1
    fmt = FORMATS[args.format]
    tracks, playlists = load_catalog(vol)
    idx, used = next_index(tracks)
    known = {t.get('source') for t in tracks if t.get('source')}
    log(f'{len(files)} file(s) found | format={args.format} '
        f'{"" if args.format!="aac" else str(args.bitrate)+" kbps"} | encoder={encoder()}')
    if args.format == 'alac':
        log('NOTE: Apple Lossless is known to cause track-skipping on the 5th-gen iPod.')
    added = skipped = failed = 0
    new_dbids = []
    for n, src in enumerate(files, 1):
        if src in known and not args.duplicates:
            skipped += 1; continue
        info = probe(src)
        if not info:
            log(f'  unreadable: {os.path.basename(src)}'); failed += 1; continue
        while idx in used: idx += 1
        used.add(idx)
        rel = 'iPod_Control/Music/F%02d/T%04d%s' % (idx % 50, idx, fmt['ext'])
        dst = os.path.join(vol, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        ok, msg = encode(src, dst, args.format, args.bitrate, info)
        if not ok:
            log(f'  FAILED {os.path.basename(src)}: {msg}'); failed += 1; continue
        sr, samples, dur, codec = measure(dst)
        size = os.path.getsize(dst)
        dbid = random.getrandbits(64)
        tracks.append(dict(
            path=':' + rel.replace('/',':'), source=src,
            title=info['title'] or os.path.basename(src), artist=info['artist'],
            album=info['album'], albumartist=info['albumartist'], genre=info['genre'],
            year=info['year'], track_nr=info['track_nr'], disc_nr=info['disc_nr'],
            size=size, tracklen=int(round(dur*1000)),
            bitrate=int(size*8/dur/1000) if dur else 0, samplerate=sr,
            samplecount=samples, dbid=dbid,
            filetype=fmt['filetype'], unk126=fmt['unk126'], unk144=fmt['unk144'],
            mediatype=1, artwork_size=0, gapless=(args.format != 'aiff')))
        new_dbids.append(dbid)
        added += 1
        if n % 10 == 0 or n == len(files):
            log(f'  {n}/{len(files)}  added={added} skipped={skipped} failed={failed}')
        if not os.path.isdir(os.path.join(vol,'iPod_Control','Music')):
            log('*** iPod disconnected — stopping, progress is saved ***'); break
    if args.playlist and new_dbids:
        pl = find_playlist(playlists, args.playlist, create=True)
        before = len(pl['dbids'])
        have = set(pl['dbids'])
        pl['dbids'].extend(d for d in new_dbids if d not in have)
        log(f'playlist "{pl["name"]}": {before} -> {len(pl["dbids"])} track(s)')
    save_catalog(vol, tracks, playlists)
    write_db(vol, tracks, args, playlists)
    log(f'DONE  added={added} already-there={skipped} failed={failed} | library={len(tracks)} tracks')
    return 0

def cmd_list(vol, args):
    tracks, _ = load_catalog(vol)
    sel = matches(tracks, args.search) if args.search else tracks
    for i, t in enumerate(sel[:args.limit]):
        log(f"{i:4d}  {t.get('artist','')} - {t.get('title','')}  [{t.get('album','')}]"
            f"  ({t.get('size',0)/1e6:.1f} MB, {t.get('filetype','')})")
    log(f'-- {len(sel)} of {len(tracks)} tracks'
        + (f' matching "{args.search}"' if args.search else ''))
    return 0

def cmd_remove(vol, args):
    tracks, playlists = load_catalog(vol)
    if not args.search and not args.dbids: log('give --search or --dbids'); return 1
    hit = matches(tracks, args.search, args.dbids)
    if not hit: log('nothing matches'); return 1
    for t in hit[:20]: log(f"  {t.get('artist','')} - {t.get('title','')}")
    if len(hit) > 20: log(f'  ... and {len(hit)-20} more')
    if not args.yes:
        if input(f'delete {len(hit)} track(s) from the iPod? [y/N] ').strip().lower() != 'y':
            log('cancelled'); return 1
    gone = {t['dbid'] for t in hit}
    for t in hit:
        p = os.path.join(vol, t['path'].strip(':').replace(':','/'))
        try: os.remove(p)
        except OSError: pass
    keep = [t for t in tracks if t['dbid'] not in gone]
    for pl in playlists:                       # drop them from every playlist too
        pl['dbids'] = [d for d in pl['dbids'] if d not in gone]
    save_catalog(vol, keep, playlists)
    write_db(vol, keep, args, playlists)
    log(f'removed {len(hit)} track(s)')
    return 0

def cmd_playlist(vol, args):
    tracks, playlists = load_catalog(vol)
    by_dbid = {t['dbid']: t for t in tracks}
    act = args.action

    if act == 'list':
        if not playlists: log('no playlists yet')
        for pl in playlists:
            log(f"  {pl['name']}  ({len(pl['dbids'])} tracks)")
        return 0

    if act == 'show':
        pl = find_playlist(playlists, args.name)
        if not pl: log(f'no playlist named "{args.name}"'); return 1
        for d in pl['dbids']:
            t = by_dbid.get(d)
            if t: log(f"  {t.get('artist','')} - {t.get('title','')}")
        log(f"-- {len(pl['dbids'])} track(s) in \"{pl['name']}\"")
        return 0

    if act == 'rename':
        pl = find_playlist(playlists, args.name)
        if not pl: log('no playlist named "%s"' % args.name); return 1
        if not args.search: log('use --search "New Name" to give the new name'); return 1
        old = pl['name']; pl['name'] = args.search
        save_catalog(vol, tracks, playlists)
        write_db(vol, tracks, args, playlists)
        log('renamed "%s" -> "%s" (%d tracks, songs untouched)' % (old, pl['name'], len(pl['dbids'])))
        return 0

    if act == 'delete':
        pl = find_playlist(playlists, args.name)
        if not pl: log(f'no playlist named "{args.name}"'); return 1
        if not args.yes and input(f'delete playlist "{pl["name"]}"? (songs are kept) [y/N] '
                                  ).strip().lower() != 'y':
            log('cancelled'); return 1
        playlists.remove(pl)
        save_catalog(vol, tracks, playlists)
        write_db(vol, tracks, args, playlists)
        log(f'playlist deleted; all {len(tracks)} songs untouched')
        return 0

    if act in ('create', 'add', 'remove'):
        if not args.search and not args.dbids:
            log('--search is required to say which tracks'); return 1
        hit = matches(tracks, args.search, args.dbids)
        if not hit: log('no tracks match'); return 1
        pl = find_playlist(playlists, args.name, create=(act in ('create','add')))
        if not pl: log(f'no playlist named "{args.name}"'); return 1
        before = len(pl['dbids'])
        if act == 'remove':
            gone = {t['dbid'] for t in hit}
            pl['dbids'] = [d for d in pl['dbids'] if d not in gone]
        else:
            have = set(pl['dbids'])
            pl['dbids'].extend(t['dbid'] for t in hit if t['dbid'] not in have)
        for t in hit[:10]:
            log(f"  {t.get('artist','')} - {t.get('title','')}")
        if len(hit) > 10: log(f'  ... and {len(hit)-10} more')
        save_catalog(vol, tracks, playlists)
        write_db(vol, tracks, args, playlists)
        log(f'"{pl["name"]}": {before} -> {len(pl["dbids"])} track(s); '
            f'library unchanged ({len(tracks)} songs)')
        return 0
    return 1

def cmd_rebuild(vol, args):
    tracks, playlists = load_catalog(vol)
    save_catalog(vol, tracks, playlists)
    write_db(vol, tracks, args, playlists)
    return 0

def cmd_info(vol, args):
    tracks, playlists = load_catalog(vol)
    total = sum(t.get('size',0) for t in tracks)
    secs  = sum(t.get('tracklen',0) for t in tracks)/1000
    kinds = {}
    for t in tracks: kinds[t.get('filetype','?')] = kinds.get(t.get('filetype','?'),0)+1
    st = os.statvfs(vol)
    log(f'iPod          : {vol}')
    log(f'tracks        : {len(tracks)}')
    log(f'formats       : ' + ', '.join(f'{k} x{v}' for k,v in kinds.items()))
    log(f'library size  : {total/1e9:.2f} GB   ({secs/3600:.1f} hours)')
    log(f'free space    : {st.f_bavail*st.f_frsize/1e9:.2f} GB')
    log(f'playlists     : ' + (', '.join(f"{p['name']} ({len(p['dbids'])})"
                                         for p in playlists) or 'none'))
    log(f'encoder       : {encoder()}   ffmpeg: {FFMPEG}')
    return 0

def write_db(vol, tracks, args, playlists=()):
    backup_db(vol)
    given = getattr(args, 'device_name', None)
    name = given if given and given != 'iPod' else CATALOG_META.get('device_name', 'iPod')
    CATALOG_META['device_name'] = name
    pos = {t['dbid']: i + 1 for i, t in enumerate(tracks)}   # track ids are 1-based positions
    extra = []
    for pl in playlists or ():
        ids = [pos[d] for d in pl['dbids'] if d in pos]
        if ids: extra.append((pl['name'], ids))
    blob = ipod_db.build_itunesdb(tracks, playlist_name=name, extra_playlists=extra)
    dbp = os.path.join(vol,'iPod_Control','iTunes','iTunesDB')
    os.makedirs(os.path.dirname(dbp), exist_ok=True)
    tmp = dbp + '.tmp'
    with open(tmp,'wb') as f:
        f.write(blob); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, dbp)
    log(f'database written: {len(tracks)} tracks, '
        f'{len(extra)+1} playlist(s), {len(blob)} bytes')

def main():
    ap = argparse.ArgumentParser(description='Put music on a click-wheel iPod.')
    ap.add_argument('--volume', help='iPod mount point (auto-detected by default)')
    ap.add_argument('--device-name', default='iPod', help='library name shown on the iPod')
    sub = ap.add_subparsers(dest='cmd', required=True)

    a = sub.add_parser('add', help='convert and add files or folders')
    a.add_argument('paths', nargs='+')
    a.add_argument('--format', choices=['aac','alac','aiff'], default='aac',
                   help='aac = recommended; alac = lossless but skips on 5G; aiff = lossless, uncompressed, huge')
    a.add_argument('--bitrate', type=int, default=256, help='AAC bitrate in kbps (default 256)')
    a.add_argument('--playlist', help='also put the newly added tracks in a playlist with this name')
    a.add_argument('--duplicates', action='store_true', help='add even if the same source file is already on the iPod')
    a.set_defaults(fn=cmd_add)

    l = sub.add_parser('list', help='show what is on the iPod')
    l.add_argument('--search'); l.add_argument('--limit', type=int, default=50)
    l.set_defaults(fn=cmd_list)

    r = sub.add_parser('remove', help='delete tracks matching text')
    r.add_argument('--search'); r.add_argument('--yes', action='store_true')
    r.add_argument('--dbids', help='comma-separated track ids (exact selection)')
    r.set_defaults(fn=cmd_remove)

    p = sub.add_parser('playlist', help='create / edit playlists (songs are never touched)')
    p.add_argument('action', choices=['list','show','create','add','remove','delete','rename'])
    p.add_argument('name', nargs='?', default='')
    p.add_argument('--search', help='which tracks to act on')
    p.add_argument('--dbids', help='comma-separated track ids (exact selection)')
    p.add_argument('--yes', action='store_true')
    p.set_defaults(fn=cmd_playlist)

    sub.add_parser('rebuild', help='regenerate the iPod database from the catalog').set_defaults(fn=cmd_rebuild)
    sub.add_parser('info', help='show library and device summary').set_defaults(fn=cmd_info)

    args = ap.parse_args()
    if not FFMPEG or not FFPROBE:
        log('ERROR: ffmpeg/ffprobe not found.  Install with:  brew install ffmpeg'); return 2
    vol = find_volume(args.volume)
    if not vol:
        log('ERROR: no iPod found. Plug it in, wait for it to mount, then retry.'); return 2
    return args.fn(vol, args)

if __name__ == '__main__':
    sys.exit(main())
