"""Minimal iTunesDB reader/writer for iPod 5th generation (and similar click-wheel iPods).
Pure Python 3, no dependencies. Structures are built from byte templates captured from a
database that is known to work on this hardware, so every 'unknown' field keeps a proven value.
"""
import base64, json, os, struct

# ---- byte templates (captured from a working iPod database) -------------------
TEMPLATES = json.loads(base64.b64decode("eyJtaGJkIjogImJXaGlaUFFBQUFCaUxCY0FBUUFBQURBQUFBQUlBQUFBZloyN2tDT2kySGdCQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUc1bEwvcW5JYzNUaXY0QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBPT0iLCAibWhzZCI6ICJiV2h6WkdBQUFBQjJ0dzRBQVFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQSIsICJtaGx0IjogImJXaHNkRndBQUFBOEF3QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQT0iLCAibWhpdCI6ICJiV2hwZEVnQ0FBQThCQUFBQndBQUFEUUFBQUFCQUFBQUFBQUFBQUFBQUFBQUFBQUFxVHBUQUhDSUFnQUJBQUFBQUFBQUFPTUhBQUFHQVFBQUFBQkVyQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFCQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUdrVFo2ZmhHdmY0QUFBQUFBRUEvLytBK1FBQUFBQUFBQUJFTEVjQUFBQUFNd0FBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFCQUFBQWFSTm5wK0VhOS9nQUFBRUFBQUFBQUFBQUFBQVl0RzhBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUVBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFFQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBUUFBQUFBQUFBQUFBQUFBcVRwVEFBQUFBQUNBZ0lDQWdJQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBR1FBQUFBQUFBQUFBUUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQVFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQT0iLCAibWhscCI6ICJiV2hzY0Z3QUFBQUNBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE9IiwgIm1oeXAiOiAiYldoNWNHd0FBQUJBMEFFQURBQUFBRHdEQUFBQkFBQUFwUFNNNWxCUFFFYUNQazU0QUFBQUFBRUFBQUFCQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBIiwgIm1oaXAiOiAiYldocGNFd0FBQUI0QUFBQUFRQUFBQUFBQUFBQUFBQUFOQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBPT0iLCAibGlzdHMiOiB7Im1obHQiOiAiYldoc2RGd0FBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBPSIsICJtaGxwIjogImJXaHNjRndBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQT0iLCAibWhsYSI6ICJiV2hzWVZ3QUFBQy9BZ0FBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE9IiwgIm1obGkiOiAiYldoc2FWd0FBQURMQWdBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBPSJ9fQ==").decode())

def _t(name):
    return bytearray(base64.b64decode(TEMPLATES[name]))

def _tl(name):
    return bytearray(base64.b64decode(TEMPLATES['lists'][name]))

MHOD_TITLE, MHOD_LOCATION, MHOD_ALBUM, MHOD_ARTIST = 1, 2, 3, 4
MHOD_GENRE, MHOD_FILETYPE, MHOD_COMMENT, MHOD_ALBUMARTIST = 5, 6, 8, 22

def u32(buf, off, val):  struct.pack_into('<I', buf, off, val & 0xFFFFFFFF)
def u16(buf, off, val):  struct.pack_into('<H', buf, off, val & 0xFFFF)
def u64(buf, off, val):  struct.pack_into('<Q', buf, off, val & 0xFFFFFFFFFFFFFFFF)

def mhod_string(mtype, text):
    """String data object."""
    raw = (text or '').encode('utf-16-le')
    body = struct.pack('<III', 1, len(raw), 0) + b'\x00' * 4 + raw
    total = 24 + len(body)
    return struct.pack('<4sIII', b'mhod', 24, total, mtype) + b'\x00' * 8 + body

def mhod_generic(mtype, payload=b''):
    total = 24 + len(payload)
    return struct.pack('<4sIII', b'mhod', 24, total, mtype) + b'\x00' * 8 + payload

def build_mhit(tr, track_id):
    """One track record: 584-byte header (templated) plus its string objects."""
    h = _t('mhit')
    mhods = []
    for mtype, key in ((MHOD_TITLE,'title'), (MHOD_LOCATION,'path'), (MHOD_ALBUM,'album'),
                       (MHOD_ARTIST,'artist'), (MHOD_GENRE,'genre'), (MHOD_FILETYPE,'filetype'),
                       (MHOD_ALBUMARTIST,'albumartist')):
        val = tr.get(key)
        if val:
            mhods.append(mhod_string(mtype, val))
    blob = b''.join(mhods)
    u32(h, 8,  584 + len(blob))         # total length
    u32(h, 12, len(mhods))              # number of data objects
    u32(h, 16, track_id)                # track id
    u32(h, 20, 1)                       # visible
    u32(h, 36, tr['size'])
    u32(h, 40, tr['tracklen'])
    u32(h, 44, tr.get('track_nr', 0))
    u32(h, 48, tr.get('total_tracks', 0))
    u32(h, 52, tr.get('year', 0))
    u32(h, 56, tr.get('bitrate', 0))
    u32(h, 60, (tr.get('samplerate', 44100) & 0xFFFF) << 16)
    u32(h, 92, tr.get('disc_nr', 0))
    u32(h, 96, tr.get('total_discs', 0))
    dbid = tr['dbid']
    u64(h, 112, dbid)
    u64(h, 168, dbid)
    art = tr.get('artwork_size', 0)
    u16(h, 124, 1 if art else 0)        # artwork present
    u16(h, 126, tr.get('unk126', 0xFFFF))
    u32(h, 128, art)
    struct.pack_into('<f', h, 136, float(tr.get('samplerate', 44100)))
    u32(h, 144, tr.get('unk144', 0x0033))
    u32(h, 184, tr.get('pregap', 0))
    u64(h, 188, tr.get('samplecount', 0))
    u32(h, 200, tr.get('postgap', 0))
    u32(h, 208, tr.get('mediatype', 1))
    u32(h, 300, tr['size'])
    u16(h, 256, 1 if tr.get("gapless", True) else 0)   # gapless playback data present
    return bytes(h) + blob

def build_playlist(name, track_ids, is_master, dbid, ordering=1):
    y = _t('mhyp')
    title = mhod_string(MHOD_TITLE, name)
    items = []
    for pos, tid in enumerate(track_ids, 1):
        ip = _t('mhip')
        child = mhod_generic(100, struct.pack('<I', pos) + b'\x00' * 16)
        u32(ip, 8, len(ip) + len(child))
        u32(ip, 12, 1)                  # one data object
        u32(ip, 16, 0)                  # podcast grouping
        u32(ip, 24, tid)                # track id
        u32(ip, 28, pos)                # timestamp slot, harmless
        items.append(bytes(ip) + child)
    blob = title + b''.join(items)
    u32(y, 8, len(y) + len(blob))
    u32(y, 12, 1)                       # number of data objects
    u32(y, 16, len(track_ids))
    u32(y, 20, 1 if is_master else 0)
    u64(y, 28, dbid)
    return bytes(y) + blob

def build_section(stype, list_name, children_blob, count):
    lst = _tl(list_name)
    u32(lst, 8, count)
    s = _t('mhsd')
    body = bytes(lst) + children_blob
    u32(s, 8, len(s) + len(body))
    u32(s, 12, stype)
    return bytes(s) + body

def build_itunesdb(tracks, playlist_name='iPod', extra_playlists=()):
    """tracks: list of dicts. Returns the complete iTunesDB as bytes."""
    trk_blob, ids = [], []
    for i, tr in enumerate(tracks):
        tid = i + 1
        ids.append(tid)
        trk_blob.append(build_mhit(tr, tid))
    sec_tracks = build_section(1, 'mhlt', b''.join(trk_blob), len(tracks))

    pls = [build_playlist(playlist_name, ids, True, 0x1234567890ABCDEF)]
    for n, (pname, pids) in enumerate(extra_playlists, 1):
        pls.append(build_playlist(pname, pids, False, 0x2000000000000000 + n))
    pl_blob = b''.join(pls)
    sec_pl3 = build_section(3, 'mhlp', pl_blob, len(pls))
    sec_pl2 = build_section(2, 'mhlp', pl_blob, len(pls))

    empties = (build_section(4, 'mhla', b'', 0) + build_section(8, 'mhli', b'', 0) +
               build_section(6, 'mhlt', b'', 0) + build_section(10, 'mhlt', b'', 0) +
               build_section(5, 'mhlp', b'', 0))
    body = sec_tracks + sec_pl3 + sec_pl2 + empties
    head = _t('mhbd')
    u32(head, 8, len(head) + len(body))
    u32(head, 20, 8)                    # eight sections
    return bytes(head) + body

# ---- reader (used for verification / migration) -------------------------------
def _walk_mhods(buf, off, count):
    out = {}
    for _ in range(count):
        if buf[off:off+4] != b'mhod': break
        hlen, tlen = struct.unpack('<II', buf[off+4:off+12])
        mtype = struct.unpack('<I', buf[off+12:off+16])[0]
        if tlen > 40:
            slen = struct.unpack('<I', buf[off+28:off+32])[0]
            try: out[mtype] = buf[off+40:off+40+slen].decode('utf-16-le')
            except Exception: pass
        off += tlen
    return out, off

def read_otg_playlists(itunes_dir):
    """On-The-Go playlists made on the iPod itself live outside the database,
    as OTGPlaylistInfo_N files. Returns [(name, [track_id, ...]), ...]."""
    import glob, re as _re
    out = []
    for p in sorted(glob.glob(os.path.join(itunes_dir, 'OTGPlaylistInfo_*'))):
        try:
            with open(p, 'rb') as f: d = f.read()
            if len(d) < 20 or d[:4] != b'mhpo': continue
            hlen, esz, count, stamp = struct.unpack('<IIII', d[4:20])
            if count <= 0 or esz != 4 or hlen + 4 * count > len(d): continue
            ids = list(struct.unpack('<%dI' % count, d[hlen:hlen + 4 * count]))
        except Exception:
            continue
        m = _re.search(r'_(\d+)$', p)
        out.append(('On-The-Go %s' % (m.group(1) if m else '?'), ids))
    return out

def read_device_name(data):
    """Title of the master playlist = the library name shown on the iPod."""
    hlen = struct.unpack('<I', data[4:8])[0]
    off = hlen
    while off < len(data) - 12 and data[off:off+4] == b'mhsd':
        shlen, stlen = struct.unpack('<II', data[off+4:off+12])
        if struct.unpack('<I', data[off+12:off+16])[0] in (2, 3):
            lhlen = struct.unpack('<I', data[off+shlen+4:off+shlen+8])[0]
            count = struct.unpack('<I', data[off+shlen+8:off+shlen+12])[0]
            p = off + shlen + lhlen
            for _ in range(count):
                if data[p:p+4] != b'mhyp': break
                yh, yt = struct.unpack('<II', data[p+4:p+12])
                nmhod = struct.unpack('<I', data[p+12:p+16])[0]
                master = struct.unpack('<I', data[p+20:p+24])[0]
                strs, _ = _walk_mhods(data, p + yh, nmhod)
                if master and strs.get(1): return strs[1]
                p += yt
            return None
        off += stlen
    return None

def read_playlists(data):
    """Returns [(name, [track_id, ...]), ...] for non-master playlists."""
    out = []
    hlen = struct.unpack('<I', data[4:8])[0]
    off = hlen
    while off < len(data) - 12 and data[off:off+4] == b'mhsd':
        shlen, stlen = struct.unpack('<II', data[off+4:off+12])
        stype = struct.unpack('<I', data[off+12:off+16])[0]
        if stype in (2, 3):
            lhlen = struct.unpack('<I', data[off+shlen+4:off+shlen+8])[0]
            count = struct.unpack('<I', data[off+shlen+8:off+shlen+12])[0]
            p = off + shlen + lhlen
            for _ in range(count):
                if data[p:p+4] != b'mhyp': break
                yh, yt = struct.unpack('<II', data[p+4:p+12])
                nmhod = struct.unpack('<I', data[p+12:p+16])[0]
                nitems = struct.unpack('<I', data[p+16:p+20])[0]
                master = struct.unpack('<I', data[p+20:p+24])[0]
                strs, q = _walk_mhods(data, p + yh, nmhod)
                ids = []
                for _ in range(nitems):
                    if data[q:q+4] != b'mhip': break
                    ih, it = struct.unpack('<II', data[q+4:q+12])
                    ids.append(struct.unpack('<I', data[q+24:q+28])[0])
                    q += it
                if not master and strs.get(1):
                    out.append((strs[1], ids))
                p += yt
            return out
        off += stlen
    return out

def read_itunesdb(data):
    tracks = []
    hlen = struct.unpack('<I', data[4:8])[0]
    off = hlen
    while off < len(data) - 12 and data[off:off+4] == b'mhsd':
        shlen, stlen = struct.unpack('<II', data[off+4:off+12])
        stype = struct.unpack('<I', data[off+12:off+16])[0]
        if stype == 1:
            lhlen = struct.unpack('<I', data[off+shlen+4:off+shlen+8])[0]
            count = struct.unpack('<I', data[off+shlen+8:off+shlen+12])[0]
            p = off + shlen + lhlen
            for _ in range(count):
                if data[p:p+4] != b'mhit': break
                mh, mt = struct.unpack('<II', data[p+4:p+12])
                nmhod = struct.unpack('<I', data[p+12:p+16])[0]
                t = {
                    'track_id': struct.unpack('<I', data[p+16:p+20])[0],
                    'size': struct.unpack('<I', data[p+36:p+40])[0],
                    'tracklen': struct.unpack('<I', data[p+40:p+44])[0],
                    'track_nr': struct.unpack('<I', data[p+44:p+48])[0],
                    'year': struct.unpack('<I', data[p+52:p+56])[0],
                    'bitrate': struct.unpack('<I', data[p+56:p+60])[0],
                    'samplerate': struct.unpack('<I', data[p+60:p+64])[0] >> 16,
                    'disc_nr': struct.unpack('<I', data[p+92:p+96])[0],
                    'dbid': struct.unpack('<Q', data[p+112:p+120])[0],
                    'artwork_size': struct.unpack('<I', data[p+128:p+132])[0],
                    'unk126': struct.unpack('<H', data[p+126:p+128])[0],
                    'unk144': struct.unpack('<I', data[p+144:p+148])[0],
                    'samplecount': struct.unpack('<Q', data[p+188:p+196])[0],
                    'mediatype': struct.unpack('<I', data[p+208:p+212])[0],
                }
                strs, _ = _walk_mhods(data, p + mh, nmhod)
                t['title'] = strs.get(1, ''); t['path'] = strs.get(2, '')
                t['album'] = strs.get(3, ''); t['artist'] = strs.get(4, '')
                t['genre'] = strs.get(5, ''); t['filetype'] = strs.get(6, '')
                t['albumartist'] = strs.get(22, '')
                tracks.append(t)
                p += mt
        off += stlen
    return tracks
