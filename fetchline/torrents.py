"""Libtorrent adapter. Each active job owns a session and an isolated payload folder."""
from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import re
import time


def library():
    try:
        import libtorrent
        return libtorrent
    except ImportError as exc:
        raise ValueError("The torrent engine is unavailable. Use the complete Fetchline Windows build or install requirements.txt.") from exc


def identities(hashes):
    result = []
    if hashes.has_v1():
        result.append("v1:" + str(hashes.v1))
    if hashes.has_v2():
        result.append("v2:" + str(hashes.v2))
    if not result:
        raise ValueError("The torrent or magnet link has no supported info hash.")
    return result


def validate_metadata(info):
    lt = library()
    files = info.files()
    if files.num_files() == 0:
        raise ValueError("This torrent contains no files.")
    for index in range(files.num_files()):
        path = files.file_path(index).replace("\\", "/")
        parts = PurePosixPath(path)
        if (parts.is_absolute() or re.match(r"^[A-Za-z]:", path) or ".." in parts.parts or
                files.file_flags(index) & lt.file_storage.flag_symlink):
            raise ValueError("Torrents with unsafe paths or symbolic links are not supported.")


def load_source(source):
    lt = library()
    try:
        if source.lower().startswith("magnet:"):
            if len(source) > 65536 or any(ord(c) < 32 for c in source):
                raise ValueError("The magnet link contains invalid characters or is too long.")
            params = lt.parse_magnet_uri(source)
            identities(params.info_hashes)
            return params, None
        file = Path(source).expanduser().resolve()
        if not file.is_file() or file.stat().st_size > 16 * 1024 * 1024:
            raise ValueError("Choose a .torrent file smaller than 16 MiB.")
        raw = file.read_bytes()
        params = lt.add_torrent_params()
        params.ti = lt.torrent_info(lt.bdecode(raw))
        validate_metadata(params.ti)
        return params, raw
    except (RuntimeError, OSError) as exc:
        raise ValueError(f"Could not read this torrent: {exc}") from exc


def session_settings(overrides=None):
    lt = library()
    from . import __version__
    settings = {"user_agent": f"DownloadEnforcer/{__version__} libtorrent/" + lt.__version__,
                "listen_interfaces": "0.0.0.0:0,[::]:0", "enable_upnp": False, "enable_natpmp": False,
                "enable_lsd": False, "enable_dht": True,
                "alert_mask": int(lt.alert.category_t.error_notification | lt.alert.category_t.status_notification | lt.alert.category_t.storage_notification),
                "connections_limit": 120}
    if overrides:
        settings.update(overrides)
    return settings


def atomic_write(path, data):
    path = Path(path)
    temporary = path.with_suffix(".tmp")
    with temporary.open("wb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def persist_alerts(session, path):
    """Drain alerts, committing resume snapshots. Return whether a save response arrived."""
    lt = library()
    saved = False
    for alert in session.pop_alerts():
        if isinstance(alert, lt.save_resume_data_alert):
            atomic_write(path, lt.write_resume_data_buf(alert.params))
            saved = True
        elif isinstance(alert, lt.save_resume_data_failed_alert):
            saved = True
    return saved


def save_and_pause(session, handle, path):
    lt = library()
    handle.pause()
    handle.save_resume_data(lt.save_resume_flags_t.flush_disk_cache | lt.save_resume_flags_t.save_info_dict)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if persist_alerts(session, path):
            return
        session.wait_for_alert(100)


def flush_completed(session, handle, path):
    """A seed can be announced before queued disk writes finish; wait for the disk barrier."""
    lt = library()
    handle.flush_cache()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        for alert in session.pop_alerts():
            if isinstance(alert, lt.save_resume_data_alert):
                atomic_write(path, lt.write_resume_data_buf(alert.params))
            if isinstance(alert, lt.cache_flushed_alert):
                return
        session.wait_for_alert(100)
    raise ValueError("The torrent finished receiving data but its disk writes have not completed. Pause and retry.")


def transfer(manager, job_id, control):
    lt = library()
    job = manager.get(job_id)
    params = None
    if Path(job.partial).is_file():
        try:
            params = lt.read_resume_data(Path(job.partial).read_bytes())
        except RuntimeError:
            params = None  # Original metadata plus piece checks safely recover a corrupt checkpoint.
    if params is None:
        params, _ = load_source(job.torrent_file or job.url)
    params.save_path = job.destination
    params.flags &= ~(lt.torrent_flags.auto_managed | lt.torrent_flags.paused)
    params.flags |= lt.torrent_flags.duplicate_is_error
    params.flags &= ~lt.torrent_flags.seed_mode
    # Always check existing files when resuming; do not trust an incomplete checkpoint blindly.
    session = lt.session(session_settings(manager.torrent_settings))
    handle = None
    completed = False
    try:
        manager._check(control)
        handle = session.add_torrent(params)
        metadata_checked = False
        completion_flushed = False
        last_save = time.monotonic()
        last_persist = 0
        limits = None
        while True:
            manager._check(control)
            persist_alerts(session, job.partial)
            status = handle.status()
            if status.errc.value():
                raise ValueError(f"Torrent error: {status.errc.message()}")
            if status.has_metadata and not metadata_checked:
                info = handle.torrent_file()
                validate_metadata(info)
                manager._update(job_id, torrent_hashes=identities(info.info_hashes()),
                                file_count=info.num_files(), total=info.total_size(), display_name=info.name(), persist=True)
                metadata_checked = True
                handle.save_resume_data(lt.save_resume_flags_t.save_info_dict)
            settings = manager.settings()
            current_limits = (settings["limit_kib"], settings["upload_limit_kib"])
            if current_limits != limits:
                handle.set_download_limit(current_limits[0] * 1024)
                handle.set_upload_limit(current_limits[1] * 1024)
                limits = current_limits
            live = manager.get(job_id)
            completed = bool(status.is_seeding)
            if completed and not completion_flushed:
                flush_completed(session, handle, job.partial)
                completion_flushed = True
            if completed and not live.seed_after_complete:
                break
            if not status.has_metadata:
                state, note = "Finding metadata", "Waiting for peers to provide the torrent's file list."
            elif int(status.state) in (0, 1, 6, 7):
                state, note = "Checking", "Checking saved pieces against the torrent hashes."
            elif completed:
                state, note = "Seeding", "Download complete. Sharing pieces until you pause or stop seeding."
            elif status.num_peers == 0:
                state, note = "Finding peers", "Waiting for peers. Availability depends on the torrent swarm."
            else:
                state, note = "Downloading", "Torrent pieces are checked as they arrive."
            now = time.monotonic()
            manager._update(job_id, state=state, downloaded=int(status.total_wanted_done),
                            total=int(status.total_wanted) if status.has_metadata else None,
                            peers=status.num_peers, seeds=status.num_seeds,
                            speed=status.download_payload_rate, upload_speed=status.upload_payload_rate,
                            uploaded=int(status.all_time_upload), note=note, error="", persist=now - last_persist >= 1)
            if now - last_persist >= 1:
                last_persist = now
            if now - last_save >= 10:
                handle.save_resume_data(lt.save_resume_flags_t.save_info_dict)
                last_save = now
            control.event.wait(0.2)
    finally:
        if handle is not None and handle.is_valid():
            save_and_pause(session, handle, job.partial)
            session.remove_torrent(handle)  # No delete_files flag: payloads stay in their own folder.
        session.pause()
    manager._check(control)
    if completed:
        live = manager.get(job_id)
        manager._update(job_id, state="Completed", downloaded=live.total or live.downloaded,
                        speed=0, upload_speed=0, peers=0, seeds=0, error="",
                        note="Torrent complete. Piece hashes verified; seeding stopped.", persist=True)
