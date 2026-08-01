"""Spotify playlist browsing and import endpoints."""

import asyncio
import logging
import os
import re
import shutil
import sqlite3
import uuid
from difflib import SequenceMatcher
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from core.dependencies import (
    get_acquisition_dispatcher,
    get_download_service,
    get_download_store,
    get_jellyfin_library_service,
    get_library_scanner,
    get_local_files_service,
    get_navidrome_library_service,
    get_plex_library_service,
    get_playlist_service,
    get_spotify_import_service,
    get_sse_publisher,
)
from services.native.download_service import ALREADY_IN_LIBRARY
from core.task_registry import TaskRegistry
from infrastructure.msgspec_fastapi import AppStruct, MsgSpecBody, MsgSpecRoute
from middleware import CurrentUserDep
from services.spotify_import_service import SpotifyImportService, SpotifyNotLinkedError

_LINK_SOURCE_PRIORITY = ["local", "jellyfin", "navidrome", "plex"]

logger = logging.getLogger(__name__)

router = APIRouter(route_class=MsgSpecRoute, prefix="/me/spotify", tags=["spotify"])


class SpotifyPlaylistItem(AppStruct):
    id: str
    name: str
    description: str
    track_count: int
    cover_url: str | None
    owner: str
    imported_playlist_id: str | None


class SpotifyPlaylistListResponse(AppStruct):
    playlists: list[SpotifyPlaylistItem]


class SpotifyImportRequest(AppStruct):
    name: str


class SpotifyImportResponse(AppStruct):
    playlist_id: str


async def _auto_link_sources(
    playlist_id: str, current_user: object, skip_ids: set | None = None
) -> None:
    playlist_service = get_playlist_service()
    jf_service = get_jellyfin_library_service()
    local_service = get_local_files_service()
    nd_service = get_navidrome_library_service()
    plex_service = get_plex_library_service()
    sources_map = await playlist_service.resolve_track_sources(
        playlist_id,
        requesting=None,
        jf_service=jf_service,
        local_service=local_service,
        nd_service=nd_service,
        plex_service=plex_service,
    )
    for track_id, sources in sources_map.items():
        if skip_ids and track_id in skip_ids:
            continue
        if not sources:
            continue
        best = next((s for s in _LINK_SOURCE_PRIORITY if s in sources), None)
        if best:
            try:
                await playlist_service.update_track_source(
                    playlist_id,
                    current_user,
                    track_id,
                    source_type=best,
                    jf_service=jf_service,
                    local_service=local_service,
                    nd_service=nd_service,
                    plex_service=plex_service,
                )
            except Exception:  # noqa: BLE001
                pass


def _norm(s: str | None) -> str:
    return (s or "").lower().strip()


def _name_match(a: str, b: str) -> bool:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    return SequenceMatcher(None, a, b).ratio() > 0.6


def _primary_artist(name: str) -> str:
    n = _norm(name)
    for sep in (",", "&", " feat", " ft.", " ft ", " with "):
        if sep in n:
            n = n.split(sep)[0]
    return n.strip()


def _artist_match(csv_artist: str, row_artist: str, row_album_artist: str) -> bool:
    primary = _primary_artist(csv_artist)
    if not primary:
        return False
    for cand in (row_artist, row_album_artist):
        c = _norm(cand)
        if not c:
            continue
        if primary in c or c in primary:
            return True
        if SequenceMatcher(None, primary, c).ratio() > 0.6:
            return True
    return False


def _clean_title(name: str) -> str:
    t = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]\s*", " ", name or "")
    low = t.lower()
    for sep in (" feat", " ft.", " ft ", " featuring "):
        i = low.find(sep)
        if i != -1:
            t = t[:i]
            low = t.lower()
    return re.sub(r"\s+", " ", t).strip()


async def _csv_local_fallback(svc: "SpotifyImportService", playlist_id: str) -> int:
    """Name-search the local catalog for tracks the MBID linker couldn't match and
    link them directly by library file id. The album-MBID path in playlist_service
    only matches tracks that resolved a release-group MBID; singles/compilations/
    feat. tracks often don't, so this catches library files by title+artist.

    Uses the library REPO (raw rows) not local_service.search_tracks: the CrateTrack
    that the service returns collapses artist to the ALBUM artist ("Various Artists"
    for soundtracks/compilations), which breaks artist matching. Raw rows keep the
    track-level artist_name. A parenthetical/feat-stripped title is tried as a second
    query so "All We Know (feat. X)" still finds a stored "All We Know"."""
    local_service = get_local_files_service()
    repo = local_service._library_repo
    tracks = await svc._async_repo.get_tracks(playlist_id)
    unmatched = [t for t in tracks if not t.source_type]
    if not unmatched:
        return 0

    linked = 0
    sem = asyncio.Semaphore(4)

    async def _one(t: object) -> None:
        nonlocal linked
        async with sem:
            cleaned = _clean_title(t.track_name)
            queries = [t.track_name]
            if cleaned and cleaned.lower() != (t.track_name or "").lower():
                queries.append(cleaned)
            titles = [x for x in (t.track_name, cleaned) if x]
            rows: list = []
            for q in queries:
                try:
                    rows = await repo.search_tracks(q, limit=15)
                except Exception:  # noqa: BLE001
                    rows = []
                if rows:
                    break
            match = None
            for r in rows:
                fid = str(r.get("id") or "")
                if not fid:
                    continue
                title = r.get("track_title") or ""
                if any(_name_match(x, title) for x in titles) and _artist_match(
                    t.artist_name, r.get("artist_name") or "", r.get("album_artist_name") or ""
                ):
                    match = fid
                    break
            if match is None:
                return
            try:
                await svc._async_repo.update_track_source(
                    playlist_id, t.id, "local", ["local"],
                    track_source_id=match, library_file_id=match,
                )
                linked += 1
            except Exception:  # noqa: BLE001
                pass

    await asyncio.gather(*(_one(t) for t in unmatched))
    return linked


_ISRC_DB = "/app/cache/library.db"


def _norm_isrc(s: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def _isrc_lookup_sync(isrcs: list[str]) -> dict[str, str]:
    if not isrcs:
        return {}
    out: dict[str, str] = {}
    try:
        conn = sqlite3.connect(_ISRC_DB, timeout=10)
    except Exception:  # noqa: BLE001
        return {}
    try:
        if not conn.execute(
            "select name from sqlite_master where type='table' and name='dn_isrc_index'"
        ).fetchone():
            return {}
        qmarks = ",".join("?" * len(isrcs))
        for isrc, fid in conn.execute(
            f"select isrc, library_file_id from dn_isrc_index where isrc in ({qmarks})",
            tuple(isrcs),
        ):
            out.setdefault(isrc, fid)
    except Exception:  # noqa: BLE001
        return {}
    finally:
        conn.close()
    return out


async def _isrc_link(
    svc: "SpotifyImportService", playlist_id: str, items: list["CsvTrackItem"]
) -> set:
    linked_ids: set = set()
    if not items:
        return linked_ids
    try:
        tracks = await svc._async_repo.get_tracks(playlist_id)
    except Exception:  # noqa: BLE001
        return linked_ids
    if len(tracks) == len(items):
        pairs = list(zip(tracks, items))
    else:
        by_key: dict[tuple[str, str], "CsvTrackItem"] = {}
        for it in items:
            by_key.setdefault((_norm(it.track_name), _norm(it.artist_name)), it)
        pairs = [
            (t, by_key[(_norm(t.track_name), _norm(t.artist_name))])
            for t in tracks
            if (_norm(t.track_name), _norm(t.artist_name)) in by_key
        ]
    wanted: dict[str, list] = {}
    for t, it in pairs:
        if getattr(t, "source_type", ""):
            continue
        ni = _norm_isrc(getattr(it, "isrc", ""))
        if ni:
            wanted.setdefault(ni, []).append(t)
    if not wanted:
        return linked_ids
    fmap = await asyncio.to_thread(_isrc_lookup_sync, list(wanted.keys()))
    for ni, ts in wanted.items():
        fid = fmap.get(ni)
        if not fid:
            continue
        for t in ts:
            try:
                await svc._async_repo.update_track_source(
                    playlist_id, t.id, "local", ["local"],
                    track_source_id=fid, library_file_id=fid,
                )
                linked_ids.add(t.id)
            except Exception:  # noqa: BLE001
                pass
    return linked_ids


async def _link_all(
    svc: "SpotifyImportService",
    playlist_id: str,
    current_user: object,
    skip_ids: set | None = None,
) -> None:
    try:
        await _auto_link_sources(playlist_id, current_user, skip_ids)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Auto-link (MBID) failed for {playlist_id}: {exc}")
    try:
        n = await _csv_local_fallback(svc, playlist_id)
        if n:
            logger.info(f"Local name-fallback linked {n} extra tracks in {playlist_id}")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Local name-fallback failed for {playlist_id}: {exc}")


async def _background_import(
    svc: SpotifyImportService,
    user_id: str,
    spotify_playlist_id: str,
    playlist_id: str,
    current_user: object,
) -> None:
    try:
        await svc.populate_playlist(user_id, spotify_playlist_id, playlist_id)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            f"Background Spotify import failed for playlist {playlist_id}: {exc}"
        )
        return
    try:
        await _auto_link_sources(playlist_id, current_user)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Auto-link failed for playlist {playlist_id}: {exc}")

    # Tell the detail/list UI the import finished so the tracks appear without a manual
    # refresh. Fires whenever populate succeeded (auto-link above is best-effort). The
    # event_id lets the client de-dupe the SSEPublisher's replay-to-new-subscribers.
    try:
        await get_sse_publisher().publish(
            f"user:{user_id}",
            "playlist_imported",
            {"playlist_id": playlist_id, "event_id": uuid.uuid4().hex},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Failed to signal Spotify import completion for {playlist_id}: {exc}")


@router.get("/playlists", response_model=SpotifyPlaylistListResponse)
async def list_spotify_playlists(
    current_user: CurrentUserDep,
    svc: SpotifyImportService = Depends(get_spotify_import_service),
) -> SpotifyPlaylistListResponse:
    try:
        playlists = await svc.list_playlists(current_user.id)
    except SpotifyNotLinkedError:
        raise HTTPException(status_code=400, detail="Spotify account not linked")
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Failed to list Spotify playlists for {current_user.id}: {exc}")
        raise HTTPException(status_code=502, detail="Failed to fetch playlists from Spotify")
    return SpotifyPlaylistListResponse(
        playlists=[
            SpotifyPlaylistItem(
                id=p["id"],
                name=p["name"],
                description=p["description"],
                track_count=p["track_count"],
                cover_url=p["cover_url"],
                owner=p["owner"],
                imported_playlist_id=p["imported_playlist_id"],
            )
            for p in playlists
        ]
    )


@router.post(
    "/playlists/{spotify_playlist_id}/import",
    response_model=SpotifyImportResponse,
)
async def import_spotify_playlist(
    spotify_playlist_id: str,
    body: SpotifyImportRequest = MsgSpecBody(SpotifyImportRequest),
    current_user: CurrentUserDep = None,
    svc: SpotifyImportService = Depends(get_spotify_import_service),
) -> SpotifyImportResponse:
    try:
        playlist_id = await svc.ensure_playlist_record(
            current_user.id, spotify_playlist_id, body.name
        )
    except SpotifyNotLinkedError:
        raise HTTPException(status_code=400, detail="Spotify account not linked")
    except Exception as exc:  # noqa: BLE001
        logger.error(
            f"Spotify import setup failed for user {current_user.id} playlist {spotify_playlist_id}: {exc}"
        )
        raise HTTPException(status_code=502, detail="Failed to start playlist import")

    task_key = f"spotify:import:{current_user.id}:{spotify_playlist_id}"
    registry = TaskRegistry.get_instance()
    if not registry.is_running(task_key):
        task = asyncio.create_task(
            _background_import(
                svc, current_user.id, spotify_playlist_id, playlist_id, current_user
            )
        )
        try:
            registry.register(task_key, task)
        except RuntimeError:
            pass

    return SpotifyImportResponse(playlist_id=playlist_id)


class CsvTrackItem(AppStruct):
    track_name: str
    artist_name: str = ""
    album_name: str = ""
    isrc: str = ""
    track_number: int | None = None
    disc_number: int | None = None
    duration: int | None = None


class CsvImportRequest(AppStruct):
    name: str
    tracks: list[CsvTrackItem]


class CsvImportResponse(AppStruct):
    playlist_id: str


def _csv_track_dict(item: "CsvTrackItem", mbid: str | None) -> dict:
    return {
        "track_name": item.track_name,
        "artist_name": item.artist_name,
        "album_name": item.album_name,
        "album_id": mbid or "",
        "source_type": "",
        "track_number": item.track_number,
        "disc_number": item.disc_number,
        "duration": item.duration,
        "cover_url": f"/api/v1/covers/release-group/{mbid}?size=250" if mbid else None,
    }


async def _publish_imported(user_id: str, playlist_id: str) -> None:
    try:
        await get_sse_publisher().publish(
            f"user:{user_id}",
            "playlist_imported",
            {"playlist_id": playlist_id, "event_id": uuid.uuid4().hex},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"SSE publish failed for {playlist_id}: {exc}")


def _isrc_map(items: list["CsvTrackItem"]) -> dict[tuple[str, str], str]:
    out: dict[tuple[str, str], str] = {}
    for it in items or []:
        ni = _norm_isrc(getattr(it, "isrc", ""))
        if ni:
            out.setdefault((_norm(it.track_name), _norm(it.artist_name)), ni)
    return out


def _apply_covers_sync(updates: list[tuple[str, str, str]]) -> int:
    if not updates:
        return 0
    try:
        conn = sqlite3.connect(_ISRC_DB, timeout=30)
    except Exception:  # noqa: BLE001
        return 0
    try:
        conn.execute("pragma busy_timeout=30000")
        conn.executemany(
            "update playlist_tracks set album_id=?, cover_url=? "
            "where id=? and (album_id is null or album_id='')",
            updates,
        )
        conn.commit()
        return len(updates)
    except Exception:  # noqa: BLE001
        return 0
    finally:
        conn.close()


def _backfill_formats_sync(playlist_id: str) -> int:
    """Copy the linked library file's format (e.g. flac) onto the playlist track so the
    row renders its format badge - update_track_source never persisted it, so even
    natively-linked playlists showed no badge."""
    try:
        conn = sqlite3.connect(_ISRC_DB, timeout=30)
    except Exception:  # noqa: BLE001
        return 0
    try:
        conn.execute("pragma busy_timeout=30000")
        n = conn.execute(
            "update playlist_tracks set format="
            "(select f.file_format from library_files f where f.id=playlist_tracks.library_file_id) "
            "where playlist_id=? and (format is null or format='') "
            "and library_file_id is not null and library_file_id<>'' "
            "and exists(select 1 from library_files f where f.id=playlist_tracks.library_file_id "
            "and f.file_format is not null and f.file_format<>'')",
            (playlist_id,),
        ).rowcount
        conn.commit()
        return n
    except Exception:  # noqa: BLE001
        return 0
    finally:
        conn.close()


def _playlist_rows_sync(playlist_id: str) -> list[dict]:
    try:
        conn = sqlite3.connect(_ISRC_DB, timeout=30)
    except Exception:  # noqa: BLE001
        return []
    try:
        conn.row_factory = sqlite3.Row
        return [
            dict(r)
            for r in conn.execute(
                "select id, track_name, artist_name, album_name, album_id, "
                "source_type, library_file_id, track_source_id "
                "from playlist_tracks where playlist_id=?",
                (playlist_id,),
            )
        ]
    except Exception:  # noqa: BLE001
        return []
    finally:
        conn.close()


def _local_rg_mbids_sync(file_ids: list[str]) -> dict[str, str]:
    ids = [f for f in {*file_ids} if f]
    if not ids:
        return {}
    try:
        conn = sqlite3.connect(_ISRC_DB, timeout=30)
    except Exception:  # noqa: BLE001
        return {}
    try:
        out: dict[str, str] = {}
        qmarks = ",".join("?" * len(ids))
        for fid, rg in conn.execute(
            f"select id, release_group_mbid from library_files where id in ({qmarks})",
            tuple(ids),
        ):
            if rg:
                out[str(fid)] = rg
        return out
    except Exception:  # noqa: BLE001
        return {}
    finally:
        conn.close()


def _cover_url(mbid: str) -> str:
    return f"/api/v1/covers/release-group/{mbid}?size=250"


async def _resolve_covers(
    svc: SpotifyImportService,
    playlist_id: str,
    items: list["CsvTrackItem"],
    user_id: str,
) -> None:
    """Fill album MBIDs / cover art, offline-first and non-wedging.

    Locally-linked tracks take their cover from the linked library file's
    release_group_mbid (offline, instant, reliable) - no MusicBrainz call. Only tracks
    with no local match fall back to the MB resolver, chunked + per-request timeout +
    written incrementally, so a stalled/starved MB lane just yields empty covers for that
    chunk instead of wedging the whole import (the old 611-wide gather did). Reads track
    rows directly (album_name is persisted), so /relink recovers already-imported
    playlists without the original CSV; ISRC is used only when the caller still has it."""
    rows = await asyncio.to_thread(_playlist_rows_sync, playlist_id)
    pending = [r for r in rows if not (r.get("album_id") or "")]
    if not pending:
        return
    imap = _isrc_map(items)
    filled = 0

    # Offline: locally-available tracks already know their release-group art.
    local = [
        (r, str(r.get("library_file_id") or r.get("track_source_id") or ""))
        for r in pending
    ]
    local = [(r, fid) for r, fid in local if fid]
    covered: set = set()
    if local:
        rgmap = await asyncio.to_thread(_local_rg_mbids_sync, [fid for _, fid in local])
        updates = [
            (rgmap[fid], _cover_url(rgmap[fid]), r["id"])
            for r, fid in local
            if fid in rgmap
        ]
        for _, _, tid in updates:
            covered.add(tid)
        if updates:
            filled += await asyncio.to_thread(_apply_covers_sync, updates)
            await _publish_imported(user_id, playlist_id)

    # MusicBrainz fallback for tracks not resolved from the local library.
    uniq: dict[tuple[str, str, str], list] = {}
    for r in pending:
        if r["id"] in covered:
            continue
        isrc = imap.get((_norm(r["track_name"]), _norm(r["artist_name"])), "")
        uniq.setdefault(
            (r.get("artist_name") or "", r.get("album_name") or "", isrc), []
        ).append(r)
    keys = [k for k in uniq if k[1] or k[2]]
    if keys:
        CHUNK, TIMEOUT = 25, 20.0
        sem = asyncio.Semaphore(4)

        async def resolve_one(key: tuple[str, str, str]):
            artist, album, isrc = key
            async with sem:
                try:
                    mbid = await asyncio.wait_for(
                        svc._resolve_mbid(isrc or None, artist, album), TIMEOUT
                    )
                except Exception:  # noqa: BLE001  (TimeoutError included)
                    mbid = None
            return key, mbid

        for i in range(0, len(keys), CHUNK):
            chunk = keys[i : i + CHUNK]
            resolved = dict(await asyncio.gather(*[resolve_one(k) for k in chunk]))
            updates = []
            for key, mbid in resolved.items():
                if not mbid:
                    continue
                for r in uniq[key]:
                    updates.append((mbid, _cover_url(mbid), r["id"]))
            if updates:
                filled += await asyncio.to_thread(_apply_covers_sync, updates)
                await _publish_imported(user_id, playlist_id)

    if filled:
        logger.info(
            f"CSV cover-resolve filled {filled} tracks in {playlist_id} "
            f"({len(covered)} local)"
        )


async def _csv_enrich(
    svc: SpotifyImportService, current_user: object, playlist_id: str, items: list["CsvTrackItem"]
) -> None:
    user_id = current_user.id
    # Phase 1 - offline local linking first, independent of MusicBrainz, so playable /
    # downloadable status lands in seconds and can never be blocked by cover lookups.
    linked: set = set()
    try:
        linked = await _isrc_link(svc, playlist_id, items)
        if linked:
            logger.info(f"ISRC-exact linked {len(linked)} tracks in {playlist_id}")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"ISRC link failed for {playlist_id}: {exc}")
    await _link_all(svc, playlist_id, current_user, skip_ids=linked)
    await asyncio.to_thread(_backfill_formats_sync, playlist_id)
    await _publish_imported(user_id, playlist_id)
    # Phase 2 - MusicBrainz cover art, chunked + timeout-bounded + written incrementally.
    try:
        await _resolve_covers(svc, playlist_id, items, user_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"CSV cover-resolve failed for {playlist_id}: {exc}")
    await _publish_imported(user_id, playlist_id)


@router.post("/import-csv", response_model=CsvImportResponse, status_code=202)
async def import_csv(
    current_user: CurrentUserDep,
    body: CsvImportRequest = MsgSpecBody(CsvImportRequest),
    svc: SpotifyImportService = Depends(get_spotify_import_service),
) -> CsvImportResponse:
    record = await svc._playlist_service.create_playlist(
        body.name or "Imported Playlist",
        source_ref=f"csv:{body.name}",
        user_id=current_user.id,
    )
    initial = [_csv_track_dict(it, None) for it in body.tracks]
    if initial:
        await svc._async_repo.add_tracks(record.id, initial)

    task_key = f"csv:enrich:{record.id}"
    registry = TaskRegistry.get_instance()
    if not registry.is_running(task_key):
        task = asyncio.create_task(
            _csv_enrich(svc, current_user, record.id, body.tracks)
        )
        try:
            registry.register(task_key, task)
        except RuntimeError:
            pass

    return CsvImportResponse(playlist_id=record.id)


class RelinkResponse(AppStruct):
    playlist_id: str
    status: str


@router.post("/relink/{playlist_id}", response_model=RelinkResponse, status_code=202)
async def relink_playlist(
    playlist_id: str,
    current_user: CurrentUserDep,
    svc: SpotifyImportService = Depends(get_spotify_import_service),
) -> RelinkResponse:
    task_key = f"csv:relink:{playlist_id}"
    registry = TaskRegistry.get_instance()

    async def _run() -> None:
        await _link_all(svc, playlist_id, current_user)
        await asyncio.to_thread(_backfill_formats_sync, playlist_id)
        await _publish_imported(current_user.id, playlist_id)
        try:
            await _resolve_covers(svc, playlist_id, [], current_user.id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Relink cover-resolve failed for {playlist_id}: {exc}")
        await _publish_imported(current_user.id, playlist_id)

    if not registry.is_running(task_key):
        task = asyncio.create_task(_run())
        try:
            registry.register(task_key, task)
        except RuntimeError:
            pass

    return RelinkResponse(playlist_id=playlist_id, status="relinking")


async def _resolve_recording_mbid(
    svc: SpotifyImportService, track: object
) -> tuple[str | None, str | None]:
    """Resolve a single recording MBID for a playlist track so we can acquire JUST that
    track (not the whole album). Prefers a recording whose release-group matches the
    track's stored album_id; else the best title-matching recording."""
    title = track.track_name or ""
    artist = track.artist_name or ""
    ct = _clean_title(title)
    pa = _primary_artist(artist)
    # Spotify titles/artists carry "- feat. X" / "(feat. X)" and comma-joined artists that
    # break MB's phrase query; try cleaned primary-artist first, then progressively rawer.
    attempts: list[tuple[str, str]] = []
    for a, t in ((pa, ct), (pa, title), (artist, ct), (artist, title)):
        if a and t and (a, t) not in attempts:
            attempts.append((a, t))
    recs: list = []
    for a, t in attempts:
        try:
            recs = await asyncio.wait_for(
                svc._mb_repo.search_recordings(a, t, limit=8), 20
            )
        except Exception:  # noqa: BLE001  (TimeoutError included)
            recs = []
        if recs:
            break
    if not recs:
        return (None, None)
    tgt = _norm(getattr(track, "album_id", "") or "")
    for r in recs:
        for g in r.release_groups or []:
            if tgt and _norm(g.release_group_mbid) == tgt:
                return (r.recording_mbid, g.release_mbid)
    for r in recs:
        if _name_match(r.title, ct) or _name_match(r.title, title):
            rel = r.release_groups[0].release_mbid if r.release_groups else None
            return (r.recording_mbid, rel)
    r0 = recs[0]
    return (r0.recording_mbid, r0.release_groups[0].release_mbid if r0.release_groups else None)


def _search_terms(track: object) -> tuple[str, str, str | None]:
    """Simple slskd-friendly terms: primary artist + parenthetical/feat-stripped title,
    and drop album when it just duplicates the title. Soulseek AND-matches every token,
    so the raw 'qaraqshy, Dj B7 o Piranhao' multi-artist string finds nothing while
    'qaraqshy MONTAGEM SANTA FE 2' matches the shared folder."""
    artist = _primary_artist(track.artist_name or "") or (track.artist_name or "")
    title = _clean_title(track.track_name or "") or (track.track_name or "")
    album = _clean_title(track.album_name or "")
    if not album or _norm(album) == _norm(title):
        album = None
    return artist, title, album


async def _bg_request_tracks(
    svc: SpotifyImportService, user_id: str, tracks: list
) -> None:
    dispatcher = get_acquisition_dispatcher()
    sem = asyncio.Semaphore(3)
    seen: set = set()
    queued = 0

    async def _one(t: object) -> None:
        nonlocal queued
        async with sem:
            rec, rel = await _resolve_recording_mbid(svc, t)
            if not rec or rec in seen:
                return
            seen.add(rec)
            _a, _ti, _al = _search_terms(t)
            try:
                await dispatcher.request_track(
                    user_id=user_id,
                    recording_mbid=rec,
                    artist_name=_a or "Unknown",
                    track_title=_ti,
                    album_title=_al,
                    duration_seconds=getattr(t, "duration", None),
                    release_group_mbid=(getattr(t, "album_id", None) or None),
                    release_mbid=rel,
                )
                queued += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"request_track failed for {getattr(t, 'track_name', '?')}: {exc}")

    await asyncio.gather(*(_one(t) for t in tracks))
    logger.info(f"Per-track request queued {queued}/{len(tracks)} for user {user_id}")


class TrackReqResponse(AppStruct):
    status: str
    count: int = 0
    task_id: str | None = None


def _is_missing(t: object) -> bool:
    return not (getattr(t, "source_type", "") or "") and bool(getattr(t, "album_id", ""))


@router.post(
    "/request-missing-tracks/{playlist_id}",
    response_model=TrackReqResponse,
    status_code=202,
)
async def request_missing_tracks(
    playlist_id: str,
    current_user: CurrentUserDep,
    svc: SpotifyImportService = Depends(get_spotify_import_service),
) -> TrackReqResponse:
    tracks = await svc._async_repo.get_tracks(playlist_id)
    missing = [t for t in tracks if _is_missing(t)]
    if missing:
        task_key = f"csv:reqtracks:{playlist_id}"
        registry = TaskRegistry.get_instance()
        if not registry.is_running(task_key):
            task = asyncio.create_task(
                _bg_request_tracks(svc, current_user.id, missing)
            )
            try:
                registry.register(task_key, task)
            except RuntimeError:
                pass
    return TrackReqResponse(status="queued", count=len(missing))


@router.post(
    "/request-track/{playlist_id}/{track_id}",
    response_model=TrackReqResponse,
)
async def request_single_track(
    playlist_id: str,
    track_id: str,
    current_user: CurrentUserDep,
    svc: SpotifyImportService = Depends(get_spotify_import_service),
) -> TrackReqResponse:
    tracks = await svc._async_repo.get_tracks(playlist_id)
    track = next((x for x in tracks if x.id == track_id), None)
    if track is None:
        raise HTTPException(status_code=404, detail="track not found")
    rec, rel = await _resolve_recording_mbid(svc, track)
    if not rec:
        return TrackReqResponse(status="no_match", count=0)
    _a, _ti, _al = _search_terms(track)
    task_id = await get_acquisition_dispatcher().request_track(
        user_id=current_user.id,
        recording_mbid=rec,
        artist_name=_a or "Unknown",
        track_title=_ti,
        album_title=_al,
        duration_seconds=getattr(track, "duration", None),
        release_group_mbid=(getattr(track, "album_id", None) or None),
        release_mbid=rel,
    )
    return TrackReqResponse(status="queued", count=1, task_id=task_id)


@router.post(
    "/check-local/{playlist_id}",
    response_model=TrackReqResponse,
    status_code=202,
)
async def check_local(
    playlist_id: str,
    current_user: CurrentUserDep,
    svc: SpotifyImportService = Depends(get_spotify_import_service),
) -> TrackReqResponse:
    """Cheap offline reconciliation fired on playlist load: name/ISRC-match the
    still-unlinked tracks against the local library so ones that have since been
    downloaded/imported flip Unknown -> Local. No MusicBrainz (won't wedge, safe to run
    every load); only publishes a refresh event when it actually links something."""
    task_key = f"csv:checklocal:{playlist_id}"
    registry = TaskRegistry.get_instance()

    async def _run() -> None:
        linked = 0
        try:
            linked = await _csv_local_fallback(svc, playlist_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"check-local failed for {playlist_id}: {exc}")
        if linked:
            try:
                await asyncio.to_thread(_backfill_formats_sync, playlist_id)
            except Exception:  # noqa: BLE001
                pass
            logger.info(f"check-local linked {linked} newly-local tracks in {playlist_id}")
            await _publish_imported(current_user.id, playlist_id)

    if not registry.is_running(task_key):
        task = asyncio.create_task(_run())
        try:
            registry.register(task_key, task)
        except RuntimeError:
            pass
    return TrackReqResponse(status="checking")


@router.post(
    "/grab-track/{playlist_id}/{track_id}",
    response_model=TrackReqResponse,
)
async def grab_track(
    playlist_id: str,
    track_id: str,
    current_user: CurrentUserDep,
    svc: SpotifyImportService = Depends(get_spotify_import_service),
    dl=Depends(get_download_service),
) -> TrackReqResponse:
    """Recording-free fallback: when MusicBrainz has no recording for a track (obscure /
    non-English / bootleg), search Soulseek directly by 'artist title' (track_count=1, no
    release-group), auto-pick the best-scoring candidate, and let DN's normal download +
    import pipeline take over. Creates a real download_task so the row's progress ring and
    completion auto-link work exactly like the MBID path."""
    tracks = await svc._async_repo.get_tracks(playlist_id)
    track = next((x for x in tracks if x.id == track_id), None)
    if track is None:
        raise HTTPException(status_code=404, detail="track not found")
    artist, title, _album = _search_terms(track)
    if not title:
        return TrackReqResponse(status="no_match")
    dur = getattr(track, "duration", None)
    try:
        job = await dl._store.create_search_job(
            user_id=current_user.id,
            artist_name=artist or "Unknown",
            album_title=title,
            year=None,
            track_count=1,
            release_group_mbid=None,
            search_query=f"{artist} - {title}",
        )
        # Force PER-FILE track matching by handing _run_search an explicit title
        # identity (no recording MBID). The folder scorer would reject the file when it
        # only exists inside its parent album's folder (e.g. "Live Your Life" under
        # "Paper Trail"); the track matcher scores the individual file instead.
        await dl._run_search(
            job.id, artist or "Unknown", title, None, 1, (None, title, dur)
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"grab-track search failed for {track_id}: {exc}")
        return TrackReqResponse(status="no_match")
    try:
        _job, candidates = await dl.get_search_job(current_user.id, job.id)
    except Exception:  # noqa: BLE001
        candidates = []
    if not candidates or (getattr(candidates[0], "final_score", 0) or 0) < 0.5:
        logger.info(f"grab-track no usable candidate for '{artist} - {title}' ({track_id})")
        return TrackReqResponse(status="no_match")
    try:
        task_id = await dl.pick_candidate(current_user.id, job.id, 0)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"grab-track pick failed for {track_id}: {exc}")
        return TrackReqResponse(status="no_match")
    logger.info(
        f"grab-track queued '{artist} - {title}' via {candidates[0].username} "
        f"(score={getattr(candidates[0], 'final_score', None)})"
    )
    return TrackReqResponse(status="queued", count=1, task_id=task_id)


_DL_DIR = "/data/downloads"
_MUSIC_DIR = "/music"


def _find_download_file(basename: str) -> str | None:
    if not basename:
        return None
    for dp, _dn, fn in os.walk(_DL_DIR):
        if "/incomplete" in dp.replace("\\", "/"):
            continue
        if basename in fn:
            return os.path.join(dp, basename)
    return None


def _safe_folder(name: str) -> str:
    name = (name or "").replace("..", "").replace("/", "").replace("\\", "").strip()
    return name or "accepted"


@router.post("/accept-quarantine/{quarantine_id}", response_model=TrackReqResponse)
async def accept_quarantine(
    quarantine_id: int,
    current_user: CurrentUserDep,
    store=Depends(get_download_store),
) -> TrackReqResponse:
    """Release a quarantined download into the library. DN quarantines slskd grabs on
    fingerprint_mismatch (common for compilation rips of the correct song) and offers no
    accept, so they never import. Locate the finished file under /data/downloads, move it
    into /music, index just that folder, and clear the record; playlists then link it on
    next load via check-local."""
    try:
        rows = await store.list_quarantine(1, 500)
    except Exception:  # noqa: BLE001
        rows = []
    row = next((r for r in rows if int(r.get("id", -1)) == quarantine_id), None)
    if not row:
        return TrackReqResponse(status="not_found")
    identity = str(row.get("identity") or "")
    remote = identity.split("\x1f")[-1].replace("\\", "/")
    basename = remote.split("/")[-1]
    parent = remote.split("/")[-2] if remote.count("/") >= 1 else "accepted"
    src = await asyncio.to_thread(_find_download_file, basename)
    if not src:
        await store.delete_quarantine(quarantine_id)  # file already gone; clear stale record
        return TrackReqResponse(status="not_found")
    dest_dir = os.path.join(_MUSIC_DIR, "_accepted", _safe_folder(parent))
    dest = os.path.join(dest_dir, basename)
    try:
        # copyfile + remove (NOT shutil.move): Zeus/music is acltype=nfsv4, so the
        # copystat inside move/copy2 raises "Operation not permitted". copyfile moves
        # only the bytes, and DN's scanner re-derives all metadata from the tags.
        await asyncio.to_thread(os.makedirs, dest_dir, 0o770, True)
        await asyncio.to_thread(shutil.copyfile, src, dest)
        await asyncio.to_thread(os.remove, src)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"accept-quarantine move failed for {quarantine_id}: {exc}")
        return TrackReqResponse(status="no_match")
    try:
        await get_library_scanner().scan([Path(dest_dir)])
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"accept-quarantine scan failed for {quarantine_id}: {exc}")
    try:
        await store.delete_quarantine(quarantine_id)
    except Exception:  # noqa: BLE001
        pass
    logger.info(f"accept-quarantine imported '{basename}' -> {dest_dir}")
    return TrackReqResponse(status="accepted")
