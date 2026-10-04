#!/usr/bin/env python3
"""Replace every song on the iPod with a 320 kbps AAC copy made from the local source.

Only the audio files are swapped, in place (same file path, same track id), so playlists,
play counts, artwork and track order stay exactly as they are. Afterwards the catalog's
size/length/bitrate fields are updated and the iTunesDB is regenerated from it.

  ipod_reencode_320.py            convert (resumable; progress in ./r320_done.jsonl)
  ipod_reencode_320.py --finalize update catalog + iTunesDB from r320_done.jsonl

Each iPod track's source file is the catalog's `source` path; tracks imported from an older iPod library have
none, so an optional aac_map.tsv (index <TAB> source file name in $RM_MUSIC_DIR <TAB> iPod path) maps them.
Tracks already at >= 315 kbps or without a local source are left alone. Back up <iPod>/iPod_Control/iTunes
and <iPod>/.ipod_sync first, and run --finalize after the conversion.
Env: RM_MUSIC_DIR (default ~/Music/FLAC), IPOD_VOL (default: auto-detect).
"""
import json, os, subprocess, sys, tempfile, time
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.getcwd()
SRC_DIR = os.path.expanduser(os.environ.get('RM_MUSIC_DIR', '~/Music/FLAC'))
_here = os.path.dirname(os.path.abspath(__file__))
for d in (os.path.join(_here, '..', 'engine', 'ipod-tool'), os.path.join(SRC_DIR, 'ipod-tool')):
    if os.path.isfile(os.path.join(d, 'ipod_sync.py')): sys.path.insert(0, os.path.abspath(d)); break
sys.argv_saved = sys.argv; sys.argv = ['ipod_sync']
import ipod_sync as S
sys.argv = sys.argv_saved

DONE = os.path.join(HERE, 'r320_done.jsonl')
MAP = os.path.join(HERE, 'aac_map.tsv')
FF, FP = S.FFMPEG, S.FFPROBE
BITRATE = 320000
TMPD = tempfile.mkdtemp(prefix='r320_')

def run(cmd): return subprocess.run(cmd, capture_output=True, text=True, timeout=900)

def stream(path):
    r = run([FP,'-v','quiet','-print_format','json','-show_streams','-select_streams','a:0',path])
    return json.loads(r.stdout)['streams'][0]

def plan(vol, tracks):
    mp = {}
    for l in (open(MAP) if os.path.exists(MAP) else []):
        p = l.rstrip('\n').split('\t')
        if len(p) == 3: mp[p[2]] = os.path.join(SRC_DIR, p[1])
    jobs, skipped = [], []
    for t in tracks:
        rel = t['path'].lstrip(':').replace(':', '/')
        dst = os.path.join(vol, rel)
        src = t.get('source', '')
        if not os.path.isfile(src): src = mp.get(rel, '')
        try: st = stream(dst)
        except Exception: skipped.append((t['title'], 'iPod file unreadable')); continue
        if int(st.get('bit_rate') or 0) >= 315000:
            skipped.append((t['title'], f"already {int(st['bit_rate'])//1000} kbps {st['codec_name']}")); continue
        if not rel.endswith('.m4a'):
            skipped.append((t['title'], 'not an .m4a slot')); continue
        if not os.path.isfile(src):
            skipped.append((t['title'], 'no local source')); continue
        jobs.append(dict(dbid=t['dbid'], rel=rel, dst=dst, src=src, title=t['title'],
                         tracklen=t.get('tracklen', 0)))
    return jobs, skipped

def convert(j):
    out = os.path.join(TMPD, '%d.m4a' % j['dbid'])
    r = run([FF,'-y','-v','error','-i',j['src'],'-map','0:a:0','-ar','44100','-vn',
             '-c:a','aac_at','-aac_at_quality','0','-b:a',str(BITRATE),
             '-movflags','+faststart','-f','ipod',out])
    if r.returncode: return j, 'encode failed: ' + r.stderr.strip()[:100], None
    try:
        st = stream(out)
        sr = int(st['sample_rate'])
        num, den = (int(x) for x in st.get('time_base', '1/%d' % sr).split('/'))
        samples = int(round(int(st['duration_ts']) * num / den * sr))
        dur = float(st['duration']); br = int(st.get('bit_rate') or 0)
        if st['codec_name'] != 'aac' or br < 300000:
            raise ValueError(f"got {st['codec_name']} {br}")
        if j['tracklen'] and abs(dur * 1000 - j['tracklen']) > 1500:
            raise ValueError(f"length differs: new {dur:.1f}s vs iPod {j['tracklen']/1000:.1f}s")
        if not os.path.isdir(os.path.join(os.path.dirname(j['dst']))):
            raise ValueError('iPod disconnected')
        part = os.path.join(os.path.dirname(j['dst']), '.r320_' + os.path.basename(j['dst']))
        with open(out, 'rb') as a, open(part, 'wb') as b:
            while True:
                buf = a.read(1 << 20)
                if not buf: break
                b.write(buf)
            b.flush(); os.fsync(b.fileno())
        os.replace(part, j['dst'])
        size = os.path.getsize(j['dst'])
        return j, 'ok', dict(dbid=j['dbid'], size=size, samplecount=samples,
                             tracklen=int(round(dur * 1000)), samplerate=sr,
                             bitrate=int(size * 8 / dur / 1000) if dur else 320)
    except Exception as e:
        return j, 'skipped: ' + str(e)[:120], None
    finally:
        try: os.remove(out)
        except OSError: pass

def finalize(vol):
    done = {}
    if os.path.exists(DONE):
        for l in open(DONE):
            d = json.loads(l); done[d['dbid']] = d
    tracks, playlists = S.load_catalog(vol)
    n = 0
    for t in tracks:
        d = done.get(t['dbid'])
        if d:
            for k in ('size', 'samplecount', 'tracklen', 'samplerate', 'bitrate'): t[k] = d[k]
            n += 1
    S.save_catalog(vol, tracks, playlists)
    class A: device_name = 'iPod'
    S.write_db(vol, tracks, A(), playlists)
    print(f'FINALIZED {n} tracks updated in catalog + iTunesDB')

def main():
    vol = S.find_volume()
    if not vol: print('ERROR: no iPod'); return 1
    if '--finalize' in sys.argv: finalize(vol); return 0
    tracks, _ = S.load_catalog(vol)
    have = set()
    if os.path.exists(DONE):
        have = {json.loads(l)['dbid'] for l in open(DONE)}
    jobs, skipped = plan(vol, tracks)
    print(f'iPod {vol}: {len(tracks)} tracks | to convert {len(jobs)} | '
          f'skipped {len(skipped)} | encoder aac_at {BITRATE//1000} kbps', flush=True)
    for s in skipped: print('  skip:', s[0], '-', s[1], flush=True)
    jobs = [j for j in jobs if j['dbid'] not in have]
    ok = bad = 0; t0 = time.time()
    with open(DONE, 'a') as log, ThreadPoolExecutor(4) as ex:
        futs = [ex.submit(convert, j) for j in jobs]
        for n, f in enumerate(as_completed(futs), 1):
            j, status, info = f.result()
            if status == 'ok':
                log.write(json.dumps(info) + '\n'); log.flush(); ok += 1
            else:
                bad += 1; print(f'  {status}  ({j["title"]})', flush=True)
            if n % 25 == 0 or n == len(jobs):
                el = time.time() - t0
                print(f'progress {n}/{len(jobs)} ok={ok} not-done={bad} '
                      f'~{int((len(jobs)-n)*el/n/60)} min left', flush=True)
    print(f'RESULT converted={ok} not-done={bad}', flush=True)
    return 0

if __name__ == '__main__':
    sys.exit(main())
