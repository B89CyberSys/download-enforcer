"""Durable download queue. The network and filesystem are independent of the GUI."""
from __future__ import annotations

import copy
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from email.utils import parsedate_to_datetime

from . import __version__

ACTIVE = {"Connecting", "Downloading", "Retrying", "Verifying", "Pausing", "Stopping",
          "Finding metadata", "Finding peers", "Checking", "Seeding"}
RETRY_HTTP = {408, 425, 429, 500, 502, 503, 504}


class DownloadError(Exception):
    """An actionable error that should be shown to the user."""


class Interrupted(Exception):
    pass


def normalize_url(value: str) -> str:
    value = value.strip()
    if len(value) > 8192 or any(ord(c) < 32 for c in value):
        raise ValueError("The link is too long or contains invalid characters.")
    try:
        parts = urllib.parse.urlsplit(value)
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
            raise ValueError("Enter a complete http:// or https:// download link.")
        if parts.username is not None or parts.password is not None:
            raise ValueError("Links containing a username or password are not supported.")
        host = parts.hostname.encode("idna").decode("ascii")
        if ":" in host:
            host = f"[{host}]"
        port = parts.port
        if port is not None:
            host += f":{port}"
        path = urllib.parse.quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
        query = urllib.parse.quote(parts.query, safe="%/:?@!$&'()*+,;=-._~")
        return urllib.parse.urlunsplit((parts.scheme.lower(), host, path, query, ""))
    except (UnicodeError, ValueError) as exc:
        raise ValueError(str(exc)) from exc


def safe_filename(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")[:160].rstrip(" .")
    if not value or value in {".", ".."}:
        value = "download.bin"
    base = value.split(".")[0].upper()
    if base in {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} or re.fullmatch(r"(?:COM|LPT)[1-9¹²³]", base):
        value = "_" + value
    return value


def filename_from_url(url: str) -> str:
    return safe_filename(urllib.parse.unquote(urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1]))


def download_filename(url: str, requested: str = "") -> str:
    original = filename_from_url(url)
    if not requested.strip():
        return original
    chosen = safe_filename(requested)
    suffix = Path(chosen).suffix
    # A dotted version number such as "Ghidra 12.14" is a name, not a file type.
    if not suffix or re.fullmatch(r"\.\d+", suffix):
        extension = Path(original).suffix
        for compound in (".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst"):
            if original.lower().endswith(compound):
                extension = compound
                break
        if extension and re.fullmatch(r"\.[A-Za-z][A-Za-z0-9.]{0,14}", extension):
            return chosen + extension
    return chosen


def validate_download_link(url: str):
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower().rstrip(".")
    youtube = host in {"youtube.com", "youtube-nocookie.com"} or host.endswith((".youtube.com", ".youtube-nocookie.com"))
    video_path = parts.path == "/watch" or parts.path.startswith(("/shorts/", "/live/", "/embed/"))
    if host == "youtu.be" or (youtube and video_path):
        raise ValueError("YouTube video downloading is not supported yet. Use a direct file link; a YouTube page link is not a video file.")


def strong_etag(value: str) -> str:
    return value if value.startswith('"') and value.endswith('"') and len(value) >= 2 else ""


def valid_date(value: str) -> str:
    try:
        return value if parsedate_to_datetime(value).tzinfo else ""
    except (TypeError, ValueError, OverflowError):
        return ""


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        newurl = normalize_url(newurl)
        if req.type == "https" and urllib.parse.urlsplit(newurl).scheme != "https":
            raise DownloadError("The server redirected a secure download to an insecure link.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@dataclass
class Job:
    id: str
    url: str
    destination: str
    partial: str
    state: str = "Queued"
    downloaded: int = 0
    total: int | None = None
    etag: str = ""
    modified: str = ""
    effective_url: str = ""
    error: str = ""
    note: str = ""
    expected_sha256: str = ""
    sha256: str = ""
    speed: float = 0
    attempts: int = 0
    created: float = 0
    kind: str = "http"
    torrent_file: str = ""
    torrent_hashes: list[str] = field(default_factory=list)
    peers: int = 0
    seeds: int = 0
    upload_speed: float = 0
    uploaded: int = 0
    seed_after_complete: bool = False
    file_count: int = 0
    display_name: str = ""
    connection_mode: str = "single"
    connections: int = 1
    acceleration_note: str = ""


@dataclass
class Control:
    event: threading.Event
    target: str = "Paused"


class QueueStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        database_existed = self.path.exists() and self.path.stat().st_size > 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.previous_version = self._stored_version()
        self.upgrade_backup = None
        if database_existed and self.previous_version != __version__:
            self.upgrade_backup = self._backup_before_upgrade()
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS job_guard (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        missing = self.db.execute(
            "SELECT COUNT(*) FROM job_guard LEFT JOIN jobs USING (id) WHERE jobs.id IS NULL"
        ).fetchone()[0]
        self.db.execute(
            "INSERT OR IGNORE INTO jobs (id, payload) SELECT id, payload FROM job_guard"
        )
        self.db.execute(
            "INSERT OR IGNORE INTO job_guard (id, payload) SELECT id, payload FROM jobs"
        )
        self.recovered_jobs = int(missing)
        self.db.commit()

    def _stored_version(self) -> str | None:
        has_settings = self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'"
        ).fetchone()
        if not has_settings:
            return None
        row = self.db.execute("SELECT value FROM settings WHERE key='app_version'").fetchone()
        if not row:
            return None
        try:
            value = json.loads(row[0])
        except (TypeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, str) else None

    def _backup_before_upgrade(self) -> Path:
        backup_dir = self.path.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        version = re.sub(r"[^0-9A-Za-z._-]", "_", __version__)
        destination = backup_dir / f"queue-before-{version}.sqlite3"
        if destination.exists():
            return destination
        target = sqlite3.connect(destination)
        try:
            self.db.backup(target)
            target.commit()
            result = target.execute("PRAGMA quick_check").fetchone()[0]
            if result != "ok":
                raise DownloadError(f"Queue backup failed its integrity check: {result}")
        finally:
            target.close()
        return destination

    def load(self) -> list[Job]:
        fields = Job.__dataclass_fields__
        jobs = []
        for row_id, payload in self.db.execute("SELECT id, payload FROM jobs"):
            values = json.loads(payload)
            if values.get("id") != row_id:
                raise DownloadError(f"Queue entry {row_id} has an inconsistent identifier.")
            jobs.append(Job(**{key: value for key, value in values.items() if key in fields}))
        return sorted(jobs, key=lambda job: job.created)

    def save(self, job: Job):
        payload = json.dumps(asdict(job))
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO jobs VALUES (?, ?)", (job.id, payload))
            self.db.execute("INSERT OR REPLACE INTO job_guard VALUES (?, ?)", (job.id, payload))

    def save_many(self, jobs):
        records = [(job.id, json.dumps(asdict(job))) for job in jobs]
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO jobs VALUES (?, ?)", records)
            self.db.executemany("INSERT OR REPLACE INTO job_guard VALUES (?, ?)", records)

    def delete(self, job_id: str):
        with self.db:
            self.db.execute("DELETE FROM jobs WHERE id=?", (job_id,))
            self.db.execute("DELETE FROM job_guard WHERE id=?", (job_id,))

    def job_ids(self) -> set[str]:
        return {row[0] for row in self.db.execute("SELECT id FROM jobs")}

    def mark_current_version(self):
        self.set_setting("app_version", __version__)

    def close(self):
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.db.close()

    def setting(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_setting(self, key: str, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, json.dumps(value)))


class DownloadManager:
    def __init__(self, database: Path, *, concurrency: int = 2, timeout: float = 10,
                 retries: int = 3, retry_delay: float = 1, autostart: bool = True):
        self.lock = threading.RLock()
        self.store = QueueStore(database)
        self.data_dir = database.resolve().parent
        self.jobs = {job.id: job for job in self.store.load()}
        self.controls: dict[str, Control] = {}
        self.threads: dict[str, threading.Thread] = {}
        self.concurrency = max(1, min(4, int(self.store.setting("concurrency", concurrency))))
        self.limit_kib = max(0, int(self.store.setting("limit_kib", 0)))
        self.upload_limit_kib = max(0, int(self.store.setting("upload_limit_kib", 128)))
        self.http_connections = max(1, min(8, int(self.store.setting("http_connections", 4))))
        self.torrent_settings = {}  # Test sessions can be restricted to loopback-only discovery.
        self.timeout, self.retries, self.retry_delay = timeout, retries, retry_delay
        self.closing = False
        self.scheduler_paused = False
        loaded_ids = set(self.jobs)
        for job in self.jobs.values():
            job.speed = job.upload_speed = 0
            job.peers = job.seeds = 0
            if job.state in ACTIVE or job.state == "Queued":
                job.state = "Paused"
                job.note = "Recovered after restart. Choose Resume to continue."
            if job.state != "Completed" and job.kind == "http":
                job.downloaded = self._partial_size(job)
        self.store.save_many(self.jobs.values())
        if self.store.job_ids() != loaded_ids:
            raise DownloadError("Queue verification failed during startup; the upgrade backup was kept.")
        self.store.mark_current_version()
        if autostart:
            self._schedule()

    @staticmethod
    def _partial_size(job):
        from .segmented import saved_progress
        progress = saved_progress(job.partial)
        if progress is not None:
            return progress
        try:
            return Path(job.partial).stat().st_size
        except FileNotFoundError:
            return 0

    def snapshot(self) -> list[Job]:
        with self.lock:
            return copy.deepcopy(list(self.jobs.values()))

    def get(self, job_id: str) -> Job:
        with self.lock:
            return copy.deepcopy(self.jobs[job_id])

    def settings(self):
        with self.lock:
            return {"concurrency": self.concurrency, "limit_kib": self.limit_kib,
                    "upload_limit_kib": self.upload_limit_kib,
                    "http_connections": self.http_connections,
                    "folder": self.store.setting("folder", str(Path.home() / "Downloads"))}

    def preference(self, key, default=None):
        with self.lock:
            return self.store.setting(key, default)

    def set_preference(self, key, value):
        with self.lock:
            self.store.set_setting(key, value)

    def configure(self, *, concurrency: int, limit_kib: int, folder: str, upload_limit_kib: int | None = None,
                  http_connections: int | None = None):
        if not 1 <= concurrency <= 4 or not 0 <= limit_kib <= 1_000_000:
            raise ValueError("Use 1–4 simultaneous downloads and a nonnegative speed limit.")
        target = Path(folder).expanduser().resolve()
        if not target.is_dir():
            raise ValueError("Choose an existing download folder.")
        if upload_limit_kib is not None and not 0 <= upload_limit_kib <= 1_000_000:
            raise ValueError("Use a nonnegative torrent upload limit.")
        if http_connections is not None and not 1 <= http_connections <= 8:
            raise ValueError("Use 1–8 connections per HTTP file.")
        with self.lock:
            self.concurrency, self.limit_kib = concurrency, limit_kib
            if upload_limit_kib is not None:
                self.upload_limit_kib = upload_limit_kib
            if http_connections is not None:
                self.http_connections = http_connections
            for key, value in {"concurrency": concurrency, "limit_kib": limit_kib,
                               "upload_limit_kib": self.upload_limit_kib, "http_connections": self.http_connections,
                               "folder": str(target)}.items():
                self.store.set_setting(key, value)
            self._schedule()

    def _unique_destination(self, folder: Path, name: str, *, exclude: str = "") -> Path:
        used = {os.path.normcase(j.destination) for j in self.jobs.values() if j.id != exclude}
        original = Path(name)
        for index in range(10000):
            suffix = "" if index == 0 else f" ({index})"
            candidate = folder / f"{original.stem}{suffix}{original.suffix}"
            if os.path.normcase(str(candidate)) not in used and not candidate.exists():
                return candidate
        raise ValueError("Too many files with that name. Choose another filename.")

    def add(self, url: str, folder: str | Path, filename: str = "", expected_sha256: str = "") -> Job:
        if url.strip().lower().startswith("magnet:"):
            if expected_sha256.strip():
                raise ValueError("Torrent data is verified using its piece hashes; leave the SHA-256 field empty.")
            return self.add_torrent(url.strip(), folder, name=filename)
        url = normalize_url(url)
        validate_download_link(url)
        expected_sha256 = expected_sha256.strip().lower()
        if expected_sha256 and not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            raise ValueError("The SHA-256 must contain exactly 64 hexadecimal characters.")
        folder = Path(folder).expanduser().resolve()
        if not folder.is_dir():
            raise ValueError("Choose an existing download folder.")
        with self.lock:
            if self.closing:
                raise ValueError("Fetchline is closing.")
            dest = self._unique_destination(folder, download_filename(url, filename))
            job_id = uuid.uuid4().hex
            partial = folder / f".fetchline-{job_id}.part"
            # Reserve only a new partial file; never adopt an existing file.
            with partial.open("xb"):
                pass
            job = Job(job_id, url, str(dest), str(partial), expected_sha256=expected_sha256, created=time.time())
            self.store.save(job)
            self.jobs[job_id] = job
            self._schedule()
            return copy.deepcopy(job)

    def add_torrent(self, source: str, folder: str | Path, *, name: str = "", seed: bool = False) -> Job:
        from .torrents import load_source, identities
        params, raw = load_source(source)
        hashes = identities(params.ti.info_hashes() if params.ti else params.info_hashes)
        target = Path(folder).expanduser().resolve()
        if not target.is_dir():
            raise ValueError("Choose an existing download folder.")
        with self.lock:
            if self.closing:
                raise ValueError("Fetchline is closing.")
            if any(set(j.torrent_hashes) & set(hashes) for j in self.jobs.values() if j.kind == "torrent"):
                raise ValueError("This torrent is already in your queue.")
            job_id = uuid.uuid4().hex
            title = safe_filename(name or (params.ti.name() if params.ti else params.name) or "Magnet download")
            destination = target / f"{title} [{job_id[:8]}]"
            destination.mkdir(exist_ok=False)
            metadata = self.data_dir / "torrents" / job_id
            metadata.mkdir(parents=True, exist_ok=False)
            torrent_file = ""
            if raw is not None:
                torrent_path = metadata / "source.torrent"
                torrent_path.write_bytes(raw)
                torrent_file = str(torrent_path)
            job = Job(job_id, source if raw is None else str(Path(source).resolve()), str(destination),
                      str(metadata / "resume.bin"), kind="torrent", torrent_file=torrent_file,
                      torrent_hashes=hashes, seed_after_complete=seed, created=time.time(),
                      total=params.ti.total_size() if params.ti else None,
                      note="Torrents can upload pieces while downloading. Seeding after completion is " + ("on." if seed else "off."))
            self.store.save(job)
            self.jobs[job_id] = job
            self._schedule()
            return copy.deepcopy(job)

    def _schedule(self):
        with self.lock:
            if self.closing or self.scheduler_paused:
                return
            for job in self.jobs.values():
                if len(self.threads) >= self.concurrency:
                    break
                if job.state != "Queued" or job.id in self.threads:
                    continue
                control = Control(threading.Event())
                self.controls[job.id] = control
                job.state = "Connecting"
                self.store.save(job)
                worker = self._torrent_worker if job.kind == "torrent" else self._worker
                thread = threading.Thread(target=worker, args=(job.id, control), daemon=True,
                                          name=f"download-{job.id[:8]}")
                self.threads[job.id] = thread
                thread.start()

    def resume(self, job_id: str):
        with self.lock:
            job = self.jobs[job_id]
            if job_id in self.threads or (job.state == "Completed" and not (job.kind == "torrent" and job.seed_after_complete)) or self.closing:
                return
            job.state, job.error, job.speed = "Queued", "", 0
            self.store.save(job)
            self._schedule()

    def set_seeding(self, job_id: str, enabled: bool):
        with self.lock:
            job = self.jobs[job_id]
            if job.kind != "torrent":
                return
            job.seed_after_complete = bool(enabled)
            self.store.save(job)
            if enabled and job.state == "Completed":
                # Completion becomes visible just before the old worker releases its
                # slot. Queue now so that its finally block can start the new session.
                job.state, job.error, job.speed = "Queued", "", 0
                self.store.save(job)
                self._schedule()

    def stop(self, job_id: str, *, cancel: bool = False):
        with self.lock:
            job = self.jobs[job_id]
            if job.state == "Completed":
                return
            target = "Canceled" if cancel else "Paused"
            if job_id in self.controls:
                control = self.controls[job_id]
                control.target = target
                control.event.set()
                job.state = "Stopping" if cancel else "Pausing"
            else:
                job.state = target
            job.speed = 0
            self.store.save(job)

    def pause_all(self):
        with self.lock:
            self.scheduler_paused = True
            for job in self.jobs.values():
                if job.state in ACTIVE or job.state == "Queued":
                    self.stop(job.id)
            self.scheduler_paused = False

    def resume_all(self):
        with self.lock:
            for job in self.jobs.values():
                if job.state == "Paused":
                    job.state, job.error = "Queued", ""
                    self.store.save(job)
            self._schedule()

    def remove(self, job_id: str):
        with self.lock:
            if job_id in self.threads:
                raise ValueError("Pause this download and wait for it to stop before removing it.")
            job = self.jobs[job_id]
            if job.kind == "http":
                Path(job.partial).unlink(missing_ok=True)
                from .segmented import clear_checkpoint
                clear_checkpoint(job.partial)
            # Torrent payloads are always retained. Removal never recursively deletes user data.
            self.store.delete(job_id)
            del self.jobs[job_id]

    def _update(self, job_id, *, persist=False, **values):
        with self.lock:
            job = self.jobs[job_id]
            for key, value in values.items():
                setattr(job, key, value)
            if persist:
                self.store.save(job)

    def _check(self, control):
        if control.event.is_set():
            raise Interrupted()

    def _torrent_worker(self, job_id: str, control: Control):
        try:
            from .torrents import transfer
            transfer(self, job_id, control)
        except Interrupted:
            self._update(job_id, state=control.target, speed=0, upload_speed=0, peers=0, seeds=0, persist=True)
        except Exception as exc:
            self._update(job_id, state=control.target if control.event.is_set() else "Error",
                         speed=0, upload_speed=0, peers=0, seeds=0,
                         error="" if control.event.is_set() else str(exc), persist=True)
        finally:
            with self.lock:
                self.controls.pop(job_id, None)
                self.threads.pop(job_id, None)
                self._schedule()

    def _worker(self, job_id: str, control: Control):
        try:
            for attempt in range(self.retries + 1):
                self._check(control)
                self._update(job_id, attempts=attempt + 1, error="")
                try:
                    self._transfer(job_id, control)
                    break
                except urllib.error.HTTPError as exc:
                    code, wait = exc.code, exc.headers.get("Retry-After", "")
                    exc.close()
                    if code not in RETRY_HTTP or attempt == self.retries:
                        raise DownloadError(f"Server returned HTTP {code}. Check the link or try again later.") from exc
                    delay = min(60, int(wait)) if wait.isdecimal() else self.retry_delay * (2 ** attempt)
                    self._retry(job_id, control, delay, f"HTTP {code}")
                except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
                    if isinstance(exc, OSError) and not isinstance(exc, (socket.timeout, ConnectionError)) and not isinstance(exc, urllib.error.URLError):
                        raise DownloadError(f"Could not read or save the file: {exc}") from exc
                    if isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, ssl.SSLCertVerificationError):
                        raise DownloadError("The server's HTTPS certificate could not be verified.") from exc
                    if attempt == self.retries:
                        raise DownloadError(f"Connection failed after {attempt + 1} attempts: {exc}") from exc
                    self._retry(job_id, control, self.retry_delay * (2 ** attempt), "Connection interrupted")
        except Interrupted:
            self._update(job_id, state=control.target, speed=0, error="", persist=True)
        except Exception as exc:
            if control.event.is_set():
                self._update(job_id, state=control.target, speed=0, error="", persist=True)
            else:
                self._update(job_id, state="Error", speed=0, error=str(exc), persist=True)
        finally:
            with self.lock:
                self.controls.pop(job_id, None)
                self.threads.pop(job_id, None)
                self._schedule()

    def _retry(self, job_id, control, delay, reason):
        self._update(job_id, state="Retrying", speed=0,
                     note=f"{reason}. Retrying in {delay:g}s…", persist=True)
        if control.event.wait(delay):
            raise Interrupted()

    def _transfer(self, job_id, control):
        from .segmented import try_transfer
        if try_transfer(self, job_id, control):
            self._finish_file(job_id, control)
        else:
            self._single_transfer(job_id, control)

    def _single_transfer(self, job_id, control):
        job = self.get(job_id)
        partial = Path(job.partial)
        offset = self._partial_size(job)
        validator = strong_etag(job.etag) or valid_date(job.modified)
        resume = bool(offset and validator and job.effective_url)
        headers = {"User-Agent": f"DownloadEnforcer/{__version__}", "Accept-Encoding": "identity"}
        if resume:
            headers.update({"Range": f"bytes={offset}-", "If-Range": validator})
        self._update(job_id, state="Connecting", speed=0)
        opener = urllib.request.build_opener(SafeRedirect())
        try:
            response = opener.open(urllib.request.Request(job.url, headers=headers), timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            if exc.code != 416 or not resume:
                raise
            exc.close()
            # Never assume a 416 proves the partial is complete. Fetch a fresh representation.
            response = opener.open(urllib.request.Request(job.url, headers={
                "User-Agent": f"DownloadEnforcer/{__version__}", "Accept-Encoding": "identity"}), timeout=self.timeout)
            resume = False
        with response:
            self._check(control)
            status = response.status
            if status not in (200, 206):
                raise DownloadError(f"Unexpected HTTP response: {status}.")
            encoding = response.headers.get("Content-Encoding", "identity").lower()
            if encoding not in ("", "identity"):
                raise DownloadError("The server ignored the request for an uncompressed download.")
            etag = response.headers.get("ETag", "")
            modified = response.headers.get("Last-Modified", "")
            effective_url = normalize_url(response.geturl())
            validate_download_link(effective_url)
            media_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if media_type in {"text/html", "application/xhtml+xml"} and Path(job.destination).suffix.lower() not in {".html", ".htm", ".xhtml"}:
                raise DownloadError("The server returned a webpage instead of a direct file. This may be a video page, login screen, or expired download link. No file was marked complete.")
            length = response.headers.get("Content-Length")
            if length is not None and not length.isdecimal():
                raise DownloadError("The server supplied an invalid file length.")
            expected_body = int(length) if length is not None else None
            note = job.acceleration_note
            if status == 206:
                if not resume:
                    raise DownloadError("The server sent a partial file without a range request.")
                match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
                if not match:
                    raise DownloadError("The server supplied an invalid resume range. No data was appended.")
                start, end, total = map(int, match.groups())
                if start != offset or end < start or end >= total or end != total - 1:
                    raise DownloadError("The server supplied the wrong resume range. No data was appended.")
                if expected_body is not None and expected_body != end - start + 1:
                    raise DownloadError("The resume range and file length disagree. No data was appended.")
                if (effective_url != job.effective_url or
                    (strong_etag(job.etag) and etag != job.etag) or
                    (not strong_etag(job.etag) and modified != job.modified) or
                    (job.total is not None and total != job.total)):
                    raise DownloadError("The remote file changed during resume. Add the link again to start fresh.")
                expected_body = end - start + 1
                mode = "ab"
                note = "Resumed from the saved position."
            else:
                total, mode = expected_body, "wb"
                if offset:
                    note = ("The server restarted the download; previous bytes were replaced safely."
                            if resume else "No reliable resume validator was available; restarted safely.")
                offset = 0
            # Truncate before committing new validators; a crash must not pair old bytes with new metadata.
            with partial.open(mode) as output:
                self._update(job_id, state="Downloading", downloaded=offset, total=total,
                             etag=etag, modified=modified, effective_url=effective_url, note=note, persist=True)
                received = 0
                checkpoint = speed_started = time.monotonic()
                speed_bytes = 0
                while True:
                    self._check(control)
                    chunk = response.read1(64 * 1024)
                    if not chunk:
                        break
                    if expected_body is not None and received + len(chunk) > expected_body:
                        raise DownloadError("The server sent more data than its declared file length.")
                    output.write(chunk)
                    received += len(chunk)
                    speed_bytes += len(chunk)
                    now = time.monotonic()
                    elapsed = max(now - speed_started, 0.001)
                    self._update(job_id, downloaded=offset + received, speed=speed_bytes / elapsed)
                    if now - checkpoint >= 0.5:
                        output.flush()
                        os.fsync(output.fileno())
                        self._update(job_id, persist=True)
                        checkpoint = now
                    with self.lock:
                        limit = self.limit_kib
                    if limit:
                        if control.event.wait(len(chunk) / (limit * 1024)):
                            raise Interrupted()
                    if elapsed >= 1:
                        speed_started, speed_bytes = time.monotonic(), 0
                output.flush()
                os.fsync(output.fileno())
            self._check(control)
            if expected_body is not None and received != expected_body:
                raise http.client.IncompleteRead(b"", expected_body - received)
        self._finish_file(job_id, control)

    def _finish_file(self, job_id, control):
        job = self.get(job_id)
        partial = Path(job.partial)
        self._update(job_id, state="Verifying", speed=0)
        digest = hashlib.sha256()
        with partial.open("rb") as file:
            while chunk := file.read(1024 * 1024):
                self._check(control)
                digest.update(chunk)
        actual = digest.hexdigest()
        if job.expected_sha256 and actual != job.expected_sha256:
            raise DownloadError("SHA-256 mismatch. The file was kept as a partial and was not marked complete.")
        self._check(control)
        with self.lock:
            live = self.jobs[job_id]
            dest = Path(live.destination)
            for _ in range(100):
                try:
                    if os.name == "nt":
                        os.rename(partial, dest)  # Windows rename fails rather than replacing a file.
                    else:
                        os.link(partial, dest)
                        partial.unlink()
                    break
                except FileExistsError:
                    dest = self._unique_destination(dest.parent, dest.name, exclude=job_id)
            else:
                raise DownloadError("The destination keeps changing. Choose another filename.")
            live.destination, live.state = str(dest), "Completed"
            live.downloaded = dest.stat().st_size
            live.total, live.sha256, live.speed, live.error = live.downloaded, actual, 0, ""
            live.note = "SHA-256 verified against your supplied checksum." if live.expected_sha256 else "Download complete. SHA-256 calculated."
            self.store.save(live)
        from .segmented import clear_checkpoint
        try:
            clear_checkpoint(job.partial)
        except OSError:
            # The verified payload has already been published successfully.
            self._update(job_id, note=live.note + " An obsolete checkpoint could not be removed.", persist=True)

    def shutdown(self):
        with self.lock:
            self.closing = True
            for control in self.controls.values():
                control.target = "Paused"
                control.event.set()
            for job in self.jobs.values():
                if job.state == "Queued":
                    job.state = "Paused"
                    self.store.save(job)
            threads = list(self.threads.values())
        for thread in threads:
            thread.join()
        with self.lock:
            self.store.close()
