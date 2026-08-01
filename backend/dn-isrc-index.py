#!/usr/bin/env python3
"""dn-isrc-index: build dn_isrc_index (normalized ISRC -> library file id) from file tags.

Runs inside the droppedneedle container. Owns its own tables (dn_isrc_index,
dn_isrc_meta) in /app/cache/library.db and never touches DroppedNeedle's schema.
The patched spotify.py reads dn_isrc_index to link playlist tracks to local files
by exact ISRC ahead of the album-MBID and fuzzy-name matchers.

  (default)  incremental: only files imported since the last watermark
  --full     reindex every non-deleted file, then set the watermark
  --stats    print coverage and exit, writing nothing
"""
import argparse
import re
import sqlite3
import time

from mutagen import File as MFile

DB = "/app/cache/library.db"
WATERMARK_KEY = "last_imported_at"


def norm_isrc(s):
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper()) or None


def read_isrc(path):
    try:
        f = MFile(path)
        if f is None or f.tags is None:
            return None
    except Exception:
        return None
    tags = f.tags
    if "TSRC" in tags:
        try:
            return norm_isrc(str(tags["TSRC"].text[0]))
        except Exception:
            return None
    val = None
    for key in tags.keys():
        if str(key).lower() == "isrc":
            val = tags[key]
            break
    if val is None:
        return None
    if isinstance(val, list):
        val = val[0] if val else None
    return norm_isrc(str(val)) if val is not None else None


def ensure(c):
    c.execute(
        "CREATE TABLE IF NOT EXISTS dn_isrc_index ("
        "library_file_id TEXT PRIMARY KEY, isrc TEXT NOT NULL, "
        "file_path TEXT, updated_at REAL NOT NULL)"
    )
    c.execute("CREATE INDEX IF NOT EXISTS idx_dn_isrc_isrc ON dn_isrc_index(isrc)")
    c.execute("CREATE TABLE IF NOT EXISTS dn_isrc_meta (k TEXT PRIMARY KEY, v TEXT)")


def get_wm(c):
    r = c.execute("select v from dn_isrc_meta where k=?", (WATERMARK_KEY,)).fetchone()
    return float(r[0]) if r else 0.0


def set_wm(c, v):
    c.execute(
        "insert into dn_isrc_meta(k,v) values(?,?) "
        "on conflict(k) do update set v=excluded.v",
        (WATERMARK_KEY, str(v)),
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--stats", action="store_true")
    args = ap.parse_args()

    c = sqlite3.connect(DB, timeout=30)
    ensure(c)
    c.commit()

    if args.stats:
        tot = c.execute(
            "select count(*) from library_files where deleted_at is null"
        ).fetchone()[0]
        idx = c.execute("select count(*) from dn_isrc_index").fetchone()[0]
        distinct = c.execute("select count(distinct isrc) from dn_isrc_index").fetchone()[0]
        dups = c.execute(
            "select count(*) from (select isrc from dn_isrc_index "
            "group by isrc having count(*)>1)"
        ).fetchone()[0]
        print(f"library files (not deleted): {tot}")
        print(f"indexed files: {idx}  ({distinct} distinct ISRCs, {dups} ISRCs -> >1 file)")
        print(f"watermark(imported_at): {get_wm(c)}")
        return

    wm = 0.0 if args.full else get_wm(c)
    rows = c.execute(
        "select id, file_path, imported_at from library_files "
        "where deleted_at is null and imported_at > ? order by imported_at",
        (wm,),
    ).fetchall()

    deleted = c.execute(
        "select id from library_files where deleted_at is not null"
    ).fetchall()
    if deleted:
        c.executemany("delete from dn_isrc_index where library_file_id=?", deleted)

    seen = linked = cleared = 0
    maxwm = wm
    now = time.time()
    for fid, path, imp in rows:
        seen += 1
        if imp and imp > maxwm:
            maxwm = imp
        isrc = read_isrc(path)
        if isrc:
            c.execute(
                "insert into dn_isrc_index(library_file_id,isrc,file_path,updated_at) "
                "values(?,?,?,?) on conflict(library_file_id) do update set "
                "isrc=excluded.isrc,file_path=excluded.file_path,updated_at=excluded.updated_at",
                (fid, isrc, path, now),
            )
            linked += 1
        else:
            cleared += c.execute(
                "delete from dn_isrc_index where library_file_id=?", (fid,)
            ).rowcount

    if args.full:
        mx = c.execute(
            "select max(imported_at) from library_files where deleted_at is null"
        ).fetchone()[0]
        if mx:
            set_wm(c, mx)
    elif maxwm > wm:
        set_wm(c, maxwm)
    c.commit()
    print(f"scanned {seen} files, indexed {linked}, cleared {cleared} stale, watermark={get_wm(c)}")


if __name__ == "__main__":
    main()
