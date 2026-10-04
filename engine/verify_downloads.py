#!/usr/bin/env python3
"""Check that a ytflac download folder actually contains the songs the playlist asked for.

  python3 verify_downloads.py <folder>            # offline: compare titles + flag outliers
  python3 verify_downloads.py <folder> --deep     # also compare durations against YouTube
                                                  # (needs yt-dlp; metadata only, no download)
"""
import json, os, re, subprocess, sys, statistics

NOISE = re.compile(r'\((?:full |official |lyric|audio|video|visuali[sz]er|hd|4k)[^)]*\)|'
                   r'\[[^\]]*\]|\|.*$|feat\.?.*$|ft\.?.*$|- *from .*$|\(from[^)]*\)',
                   re.I)
def norm(s):
    s = NOISE.sub(' ', s or '')
    s = re.sub(r'[^\w\s]', ' ', s.lower())
    return [w for w in s.split() if w not in
            ('the','a','an','of','version','song','official','full','video','audio','mix')]

def contained(a, b):
    """fraction of a's words present in b"""
    if not a: return 1.0
    sb = set(b)
    return sum(1 for w in a if w in sb) / len(a)

def probe(path):
    r = subprocess.run(['ffprobe','-v','quiet','-print_format','json','-show_format',path],
                       capture_output=True, text=True, timeout=60)
    try: d = json.loads(r.stdout)
    except Exception: return {}, 0.0
    f = d.get('format', {})
    return {k.lower(): v for k, v in (f.get('tags') or {}).items()}, float(f.get('duration') or 0)

def yt_meta(vid):
    try:
        r = subprocess.run(['yt-dlp','--skip-download','--no-warnings','--print','%(title)s\t%(duration)s',
                            'https://www.youtube.com/watch?v=' + vid],
                           capture_output=True, text=True, timeout=90)
        t, _, d = (r.stdout.strip().split('\n')[-1] if r.stdout.strip() else '').partition('\t')
        return t, float(d or 0)
    except Exception:
        return '', 0.0

def main():
    folder = os.path.expanduser(sys.argv[1] if len(sys.argv) > 1 else '.')
    deep = '--deep' in sys.argv
    man = json.load(open(os.path.join(folder, '.ytflac-manifest.json')))
    rows, years = [], []
    for vid, e in man.items():
        if e.get('status') != 'done': continue
        files = e.get('files') or []
        fn = files[0] if files else None
        if isinstance(fn, dict): fn = fn.get('path') or fn.get('name')
        if not fn: continue
        p = fn if os.path.isabs(fn) else os.path.join(folder, os.path.basename(str(fn)))
        if not os.path.exists(p): continue
        tags, dur = probe(p)
        yr = re.search(r'(19|20)\d{2}', str(tags.get('date') or tags.get('year') or ''))
        yr = int(yr.group(0)) if yr else 0
        if yr: years.append(yr)
        rows.append(dict(vid=vid, ytitle=e.get('title',''), file=os.path.basename(p),
                         title=tags.get('title',''), artist=tags.get('artist',''),
                         year=yr, dur=dur, isrc=tags.get('isrc','')))
    med = statistics.median(years) if years else 0
    for r in rows:
        yt, ft = norm(r['ytitle']), norm(r['title'])
        r['fwd'] = contained(yt, ft)          # does the file cover what YouTube asked for?
        r['rev'] = contained(ft, yt)          # does the file add words YouTube never mentioned?
        risk = []
        if r['fwd'] < 0.6:  risk.append('title mismatch')
        if r['rev'] < 0.6:  risk.append('file title has extra words')
        if med and r['year'] and med - r['year'] >= 8: risk.append('much older than playlist')
        r['risk'] = risk
    if deep:
        print('fetching YouTube durations (metadata only)…')
        for r in rows:
            t, d = yt_meta(r['vid'])
            r['ytdur'] = d
            if d and r['dur'] and abs(d - r['dur']) > 20:
                r['risk'].append('duration off by %ds' % abs(int(d - r['dur'])))
    ok  = [r for r in rows if not r['risk']]
    bad = [r for r in rows if r['risk']]
    print('\n%d track(s) checked — %d look right, %d need a look\n' % (len(rows), len(ok), len(bad)))
    for r in bad:
        print('SUSPECT  %s' % r['ytitle'][:70])
        print('    got  %s — %s (%d, %d:%02d)' % (r['artist'][:40], r['title'][:45], r['year'],
                                                  r['dur']//60, r['dur']%60))
        if r.get('ytdur'): print('    youtube duration %d:%02d' % (r['ytdur']//60, r['ytdur']%60))
        print('    why  %s' % ', '.join(r['risk']))
        print('    link https://www.youtube.com/watch?v=%s\n' % r['vid'])
    if ok:
        print('Verified OK:')
        for r in ok:
            print('  %-42s  %s' % (r['title'][:42], r['artist'][:40]))
    fails = [(v, e.get('title','')) for v, e in man.items() if e.get('status') != 'done']
    if fails:
        print('\nStill missing (%d):' % len(fails))
        for v, t in fails: print('  %s   https://www.youtube.com/watch?v=%s' % (t[:60], v))

if __name__ == '__main__':
    main()
