#!/usr/bin/env python3
"""dn-isrc-enrich: author ISRC tags for library files that lack one.

Source priority per file:
  1. IFPI/SoundExchange registry (authoritative, artist+title search)   [primary]
  2. AcoustID fingerprint -> MusicBrainz recording ISRCs                 [fallback]
  3. stored library_files.recording_mbid -> MusicBrainz                  [last resort]
Multi-result candidates are disambiguated by artist match + duration (+/-5s vs the file).

Modes:
  (default)   DRY RUN over untagged files (resolve + report, write nothing)
  --apply     write tags + dn_isrc_index (run as uid 3000)
  --sample N  validate: re-resolve N already-indexed files, compare to their known ISRC
  --limit N   cap files processed

Reuses DroppedNeedle only to decrypt the AcoustID key. SoundExchange needs no key -
an anonymous Django session cookie obtained by one warm-up GET.
"""
import argparse
import json
import re
import sqlite3
import subprocess
import sys
import time
from difflib import SequenceMatcher
from pathlib import Path

import httpx

sys.path.insert(0, "/app")

DB = "/app/cache/library.db"
MIN_SCORE = 0.70
DUR_TOL = 5.0
MB_UA = "dn-isrc-enrich/2.0 ( https://music.aneeshtigga.com )"
SX_URL = "https://isrc-api.soundexchange.com/api/ext/recordings"
SX_ORIGIN = "https://isrcsearch.ifpi.org"
MB_SLEEP = 1.2
AID_SLEEP = 0.5
SX_SLEEP = 0.4


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def norm_isrc(s):
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper()) or None


def primary_artist(name):
    n = (name or "").lower()
    for sep in (",", "&", " feat", " ft.", " ft ", " with ", ";", " x "):
        if sep in n:
            n = n.split(sep)[0]
    return norm(n)


def artist_match(a, b):
    pa, pb = primary_artist(a), primary_artist(b)
    if not pa or not pb:
        return False
    if pa in pb or pb in pa:
        return True
    return SequenceMatcher(None, pa, pb).ratio() > 0.6


def clean_title(name):
    t = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]\s*", " ", name or "")
    low = t.lower()
    for sep in (" feat", " ft.", " ft ", " featuring "):
        i = low.find(sep)
        if i != -1:
            t = t[:i]
    return re.sub(r"\s+", " ", t).strip()


def parse_dur(s):
    if not s:
        return None
    parts = str(s).split(":")
    try:
        parts = [int(p) for p in parts]
    except ValueError:
        return None
    sec = 0
    for p in parts:
        sec = sec * 60 + p
    return sec


def get_key():
    from infrastructure.crypto import init_crypto
    init_crypto(Path("/app/config"))
    from core.dependencies.cache_providers import get_preferences_service
    return get_preferences_service().get_library_settings_raw().acoustid_api_key


def sx_warm(c):
    for url in ("https://isrc-api.soundexchange.com/", SX_URL):
        try:
            c.get(url)
        except Exception:
            pass
        if c.cookies.get("sessionid"):
            break


def sx_client():
    c = httpx.Client(timeout=25, follow_redirects=True, headers={
        "accept": "application/json", "content-type": "application/json",
        "origin": SX_ORIGIN, "referer": SX_ORIGIN + "/",
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
    })
    sx_warm(c)
    return c


def sx_search(client, artist, title):
    body = {"searchFields": {"recordingArtistName": {"value": artist},
            "recordingTitle": {"value": title}, "releaseName": {"value": ""}},
            "start": 0, "number": 15, "showReleases": False}
    for attempt in (1, 2):
        try:
            r = client.post(SX_URL, json=body)
        except Exception:
            return []
        if r.status_code == 200:
            return r.json().get("recordings") or []
        if r.status_code in (401, 403) and attempt == 1:
            sx_warm(client)
            continue
        return []
    return []


_BAD_VERSION = re.compile(
    r"karaoke|tribute|\bcover\b|made famous|originally performed|"
    r"in the style of|instrumental version|backing track", re.I)


def sx_resolve(client, artist, title, dur_sec):
    cands = sx_search(client, artist, title)
    if not cands:
        ct = clean_title(title)
        if ct and ct.lower() != (title or "").lower():
            time.sleep(SX_SLEEP)
            cands = sx_search(client, artist, ct)
    scored = []
    for r in cands:
        isrc = norm_isrc(r.get("isrc"))
        if not isrc or str(r.get("isValidIsrc")) != "True":
            continue
        ra = r.get("recordingArtistName") or ""
        if _BAD_VERSION.search(ra) or _BAD_VERSION.search(r.get("recordingVersion") or ""):
            continue
        am = artist_match(artist, ra)
        d = parse_dur(r.get("duration"))
        dd = abs(d - dur_sec) if (d and dur_sec) else 999
        scored.append((not am, dd, isrc, r.get("recordingArtistName"), r.get("recordingYear")))
    if not scored:
        return None, None
    scored.sort()
    bad_artist, dd, isrc, a, yr = scored[0]
    if bad_artist:
        return None, f"sx: no artist match ({len(scored)} cands)"
    if dd > DUR_TOL and len(scored) > 1:
        return None, f"sx: ambiguous, best dur off {dd:.0f}s"
    return isrc, f"IFPI/SX {a} {yr} durΔ{dd:.0f}s"


def fpcalc(path):
    try:
        out = subprocess.run(["fpcalc", "-json", path], capture_output=True, text=True, timeout=90)
    except Exception as e:
        return None, None, str(e)
    if out.returncode != 0:
        return None, None, (out.stderr or "fpcalc failed").strip()
    d = json.loads(out.stdout)
    return d.get("fingerprint"), int(d.get("duration") or 0), None


def acoustid_recordings(client, key, fp, dur):
    r = client.get("https://api.acoustid.org/v2/lookup",
                   params={"client": key, "meta": "recordings", "fingerprint": fp, "duration": dur},
                   timeout=25)
    r.raise_for_status()
    results = r.json().get("results") or []
    if not results:
        return []
    best = max(results, key=lambda x: x.get("score") or 0.0)
    score = best.get("score") or 0.0
    return [(rec.get("id"), score) for rec in (best.get("recordings") or []) if rec.get("id")]


def mb_isrcs(client, mbid):
    r = client.get(f"https://musicbrainz.org/ws/2/recording/{mbid}",
                   params={"inc": "isrcs", "fmt": "json"},
                   headers={"User-Agent": MB_UA}, timeout=25)
    if r.status_code != 200:
        return []
    return [norm_isrc(x) for x in (r.json().get("isrcs") or []) if norm_isrc(x)]


def acoustid_mb_resolve(http, key, path, stored_rec):
    if not key:
        return None, "no acoustid key"
    fp, dur, err = fpcalc(path)
    if fp:
        time.sleep(AID_SLEEP)
        try:
            recs = acoustid_recordings(http, key, fp, dur)
        except Exception as e:
            recs = []
            err = f"acoustid: {e}"
        for rid, score in recs[:3]:
            if score < MIN_SCORE:
                break
            time.sleep(MB_SLEEP)
            ils = mb_isrcs(http, rid)
            if ils:
                return ils[0], f"acoustid rec={rid} score={score:.2f}"
    if stored_rec:
        time.sleep(MB_SLEEP)
        ils = mb_isrcs(http, stored_rec)
        if ils:
            return ils[0], f"rec_mbid={stored_rec}"
    return None, (err or "no confident recording / no ISRC")


def resolve(sx, http, key, title, artist, dur_sec, stored_rec, path):
    isrc, why = sx_resolve(sx, artist, title, dur_sec)
    if isrc:
        return isrc, why
    time.sleep(SX_SLEEP)
    isrc, why2 = acoustid_mb_resolve(http, key, path, stored_rec)
    return isrc, (why2 if isrc else f"{why or 'sx miss'}; {why2}")


def write_isrc(path, isrc):
    from mutagen import File as MFile
    from mutagen.id3 import ID3, TSRC, ID3NoHeaderError
    from mutagen.mp4 import MP4
    ext = path.lower().rsplit(".", 1)[-1]
    if ext in ("flac", "ogg", "oga", "opus"):
        f = MFile(path)
        f["isrc"] = [isrc]
        f.save()
    elif ext == "mp3":
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            tags = ID3()
        tags.setall("TSRC", [TSRC(encoding=3, text=[isrc])])
        tags.save(path)
    elif ext in ("m4a", "mp4", "m4b"):
        f = MP4(path)
        f["----:com.apple.iTunes:ISRC"] = [isrc.encode()]
        f.save()
    else:
        raise ValueError(f"unsupported extension: {ext}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    try:
        key = get_key()
    except Exception as e:
        key = ""
        print(f"(warn: acoustid key unavailable: {e})")

    c = sqlite3.connect(DB, timeout=30)
    sx = sx_client()
    print("SX session cookies:", "yes" if sx.cookies else "NONE")
    http = httpx.Client(timeout=25)

    if args.sample:
        rows = c.execute(
            "select f.track_title, f.artist_name, f.duration_seconds, f.recording_mbid, "
            "f.file_path, i.isrc from library_files f join dn_isrc_index i on i.library_file_id=f.id "
            "where f.deleted_at is null order by f.imported_at desc limit ?", (args.sample,)).fetchall()
        match = diff = miss = 0
        for title, artist, dur, rec, path, known in rows:
            isrc, why = resolve(sx, http, key, title, artist, dur, rec, path)
            if not isrc:
                miss += 1
                verdict = "MISS"
            elif isrc == known:
                match += 1
                verdict = "MATCH"
            else:
                diff += 1
                verdict = f"DIFF (file={known})"
            print(f"  [{verdict}] {artist} - {title}  -> {isrc}  ({why})")
            time.sleep(SX_SLEEP)
        print(f"\nsample {len(rows)}: MATCH {match}, DIFF {diff}, MISS {miss}")
        return

    dry = not args.apply
    rows = c.execute(
        "select f.id, f.file_path, f.recording_mbid, f.track_title, f.artist_name, f.duration_seconds "
        "from library_files f where f.deleted_at is null "
        "and f.id not in (select library_file_id from dn_isrc_index) order by f.artist_name").fetchall()
    if args.limit:
        rows = rows[: args.limit]
    print(f"{'DRY RUN' if dry else 'APPLY'} - {len(rows)} untagged files\n")
    wrote = 0
    for fid, path, stored_rec, title, artist, dur in rows:
        isrc, why = resolve(sx, http, key, title, artist, dur, stored_rec, path)
        if not isrc:
            print(f"SKIP  {artist} - {title}\n      {why}")
            continue
        verb = "WOULD WRITE" if dry else "WRITE"
        print(f"{verb}  {artist} - {title}\n      ISRC={isrc}  via {why}")
        if not dry:
            try:
                write_isrc(path, isrc)
                c.execute("insert into dn_isrc_index(library_file_id,isrc,file_path,updated_at) "
                          "values(?,?,?,?) on conflict(library_file_id) do update set "
                          "isrc=excluded.isrc,file_path=excluded.file_path,updated_at=excluded.updated_at",
                          (fid, isrc, path, time.time()))
                c.commit()
                wrote += 1
            except Exception as e:
                print(f"      ERROR writing: {e}")
        time.sleep(SX_SLEEP)
    print(f"\n{'(dry run - nothing written)' if dry else f'wrote {wrote}'}")


if __name__ == "__main__":
    main()
