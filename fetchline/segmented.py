"""Conditional HTTP ranges with parallel, nonoverlapping writes and durable checkpoints.

A preallocated partial file is never treated as complete by its length. The checkpoint
records only prefixes flushed to disk for each segment. A resume probes the remote
representation again before trusting any of those prefixes.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import http.client
import json
import os
from pathlib import Path
import re
import threading
import time
import urllib.error
import urllib.request
from email.utils import parsedate_to_datetime

from . import __version__
from .engine import DownloadError, Interrupted, RETRY_HTTP, SafeRedirect, normalize_url, strong_etag, validate_download_link

MIN_FILE_SIZE = 8 * 1024 * 1024
MIN_SEGMENT_SIZE = 1024 * 1024
HEADERS = {"User-Agent": f"DownloadEnforcer/{__version__}", "Accept-Encoding": "identity"}


class RangeRejected(Exception):
    """The origin cannot safely supply parallel ranges; restart as one stream."""


def checkpoint_path(partial):
    return Path(str(partial) + ".segments.json")


def read_checkpoint(partial):
    path = checkpoint_path(partial)
    if not path.is_file():
        return None
    try:
        if path.stat().st_size > 65536:
            return None
        plan = json.loads(path.read_text(encoding="utf-8"))
        total = plan["total"]
        segments = plan["segments"]
        if plan["version"] != 1 or type(total) is not int or total < 1 or not 2 <= len(segments) <= 8:
            return None
        cursor = 0
        for segment in segments:
            start, end, done = segment["start"], segment["end"], segment["done"]
            if (not all(type(v) is int for v in (start, end, done)) or start != cursor or
                    end < start or not 0 <= done <= end - start + 1):
                return None
            cursor = end + 1
        if cursor != total:
            return None
        return plan
    except (OSError, ValueError, KeyError, TypeError):
        return None


def saved_progress(partial):
    plan = read_checkpoint(partial)
    if plan is None:
        return 0 if checkpoint_path(partial).exists() else None
    try:
        if Path(partial).stat().st_size != plan["total"]:
            return 0
    except FileNotFoundError:
        return 0
    return sum(s["done"] for s in plan["segments"])


def clear_checkpoint(partial):
    for path in (checkpoint_path(partial), checkpoint_path(partial).with_suffix(".tmp")):
        path.unlink(missing_ok=True)


def write_checkpoint(partial, plan):
    path = checkpoint_path(partial)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as output:
        json.dump(plan, output)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def validator(headers):
    etag = headers.get("ETag", "")
    if etag:
        return strong_etag(etag), "ETag"
    # An HTTP-date is suitable for If-Range only when strong enough (RFC 9110 8.8.2.2).
    modified = headers.get("Last-Modified", "")
    try:
        date = parsedate_to_datetime(headers.get("Date", ""))
        last = parsedate_to_datetime(modified)
        if date.tzinfo and last.tzinfo and (date - last).total_seconds() >= 60:
            return modified, "Last-Modified"
    except (ValueError, TypeError, OverflowError):
        pass
    return "", ""


def check_representation(response, job, url, token, token_header):
    effective = normalize_url(response.geturl())
    validate_download_link(effective)
    media = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    if media in {"text/html", "application/xhtml+xml"} and Path(job.destination).suffix.lower() not in {".html", ".htm", ".xhtml"}:
        raise RangeRejected("The server returned a webpage.")
    if (effective != url or response.headers.get(token_header, "") != token or
            response.headers.get("Content-Encoding", "identity").lower() not in {"", "identity"}):
        raise RangeRejected("The server changed the file or its encoding.")


def check_range(response, start, end, total):
    if response.status != 206:
        raise RangeRejected("The server did not honor a range request.")
    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
    if not match or tuple(map(int, match.groups())) != (start, end, total):
        raise RangeRejected("The server supplied an inconsistent byte range.")
    length = response.headers.get("Content-Length")
    if length is not None and (not length.isdecimal() or int(length) != end - start + 1):
        raise RangeRejected("The server supplied an inconsistent range length.")


def probe(manager, job):
    opener = urllib.request.build_opener(SafeRedirect())
    with opener.open(urllib.request.Request(job.url, headers=HEADERS, method="HEAD"), timeout=manager.timeout) as response:
        length = response.headers.get("Content-Length", "")
        token, token_header = validator(response.headers)
        if response.status != 200 or not length.isdecimal() or int(length) < MIN_FILE_SIZE or not token:
            return None
        total = int(length)
        url = normalize_url(response.geturl())
        check_representation(response, job, url, token, token_header)
    request = urllib.request.Request(job.url, headers={**HEADERS, "Range": "bytes=0-0", "If-Range": token})
    with opener.open(request, timeout=manager.timeout) as response:
        check_representation(response, job, url, token, token_header)
        check_range(response, 0, 0, total)
        if len(response.read(2)) != 1:
            raise RangeRejected("The server supplied an invalid range probe.")
    return {"url": url, "total": total, "validator": token, "validator_header": token_header}


def pause_wait(seconds, control, abort):
    deadline = time.monotonic() + seconds
    while True:
        if control.event.is_set():
            raise Interrupted()
        if abort.is_set():
            raise Interrupted()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        abort.wait(min(0.1, remaining))


class SharedLimiter:
    """One budget per file, shared by all range workers."""
    def __init__(self, manager, control, abort):
        self.manager, self.control, self.abort = manager, control, abort
        self.lock = threading.Lock()
        self.next_time = 0
        self.last_limit = None

    def consume(self, length):
        with self.lock:
            limit = self.manager.limit_kib
            if not limit:
                self.next_time = 0
                return
            if limit != self.last_limit:
                self.next_time = time.monotonic()
                self.last_limit = limit
            self.next_time = max(time.monotonic(), self.next_time) + length / (limit * 1024)
            deadline = self.next_time
        while time.monotonic() < deadline:
            if self.manager.limit_kib != limit:
                return
            pause_wait(min(0.1, deadline - time.monotonic()), self.control, self.abort)


def try_transfer(manager, job_id, control):
    job = manager.get(job_id)
    old = read_checkpoint(job.partial)
    configured = manager.http_connections
    if old is None and checkpoint_path(job.partial).exists():
        clear_checkpoint(job.partial)
        with Path(job.partial).open("wb"):
            pass
    try:
        partial_length = Path(job.partial).stat().st_size
    except FileNotFoundError:
        partial_length = 0
    if not old and (configured == 1 or partial_length > 0):
        manager._update(job_id, connection_mode="single", connections=1,
                        acceleration_note="Single connection selected." if configured == 1 else "Resuming the existing single-connection download.")
        return False
    manager._check(control)
    try:
        metadata = probe(manager, job)
    except (urllib.error.URLError, http.client.HTTPException, OSError, RangeRejected):
        metadata = None
    manager._check(control)
    if metadata is None:
        if old or checkpoint_path(job.partial).exists():
            clear_checkpoint(job.partial)
            with Path(job.partial).open("wb"):
                pass
        manager._update(job_id, connection_mode="single", connections=1,
                        acceleration_note="Using one connection: this server or file does not support safe acceleration.", persist=True)
        return False
    compatible = old and all(old.get(key) == value for key, value in metadata.items())
    if compatible and partial_length == metadata["total"]:
        plan = old
    else:
        count = max(2, min(configured, metadata["total"] // MIN_SEGMENT_SIZE))
        # A saved plan may need a safe fresh start even if the current setting is one connection.
        plan = {**metadata, "version": 1, "segments": [
            {"start": i * metadata["total"] // count, "end": (i + 1) * metadata["total"] // count - 1, "done": 0}
            for i in range(count)]}
        # Invalidate the old durable prefixes before replacing their underlying data.
        clear_checkpoint(job.partial)
        with Path(job.partial).open("wb") as output:
            output.truncate(metadata["total"])
            output.flush()
            os.fsync(output.fileno())
        write_checkpoint(job.partial, plan)
    workers = min(configured, len(plan["segments"]))
    manager._update(job_id, state="Downloading", total=plan["total"], downloaded=sum(s["done"] for s in plan["segments"]),
                    etag=plan["validator"] if plan["validator_header"] == "ETag" else "",
                    modified=plan["validator"] if plan["validator_header"] == "Last-Modified" else "",
                    effective_url=plan["url"], connection_mode="segmented", connections=workers,
                    acceleration_note=f"Parallel ranges: {workers} connection{'s' if workers != 1 else ''}.",
                    note="Downloading file parts in parallel; saved parts are checked against the server's file version.", persist=True)
    abort = threading.Event()
    guard = threading.Lock()
    limiter = SharedLimiter(manager, control, abort)
    positions = [s["done"] for s in plan["segments"]]
    initial = sum(positions)
    started = time.monotonic()

    def record(index, done, durable=False):
        with guard:
            positions[index] = done
            if durable:
                plan["segments"][index]["done"] = done
                write_checkpoint(job.partial, plan)
            received = sum(positions)
            manager._update(job_id, downloaded=received, speed=(received - initial) / max(.001, time.monotonic() - started), persist=durable)

    def segment(index):
        bounds = plan["segments"][index]
        done = bounds["done"]
        size = bounds["end"] - bounds["start"] + 1
        if done == size:
            return
        for attempt in range(manager.retries + 1):
            pause_wait(0, control, abort)
            start = bounds["start"] + done
            headers = {**HEADERS, "Range": f"bytes={start}-{bounds['end']}", "If-Range": plan["validator"]}
            try:
                opener = urllib.request.build_opener(SafeRedirect())
                with opener.open(urllib.request.Request(job.url, headers=headers), timeout=manager.timeout) as response:
                    check_representation(response, job, plan["url"], plan["validator"], plan["validator_header"])
                    check_range(response, start, bounds["end"], plan["total"])
                    checkpoint_time = time.monotonic()
                    with Path(job.partial).open("r+b", buffering=0) as output:
                        output.seek(start)
                        try:
                            while done < size:
                                pause_wait(0, control, abort)
                                chunk = response.read1(min(65536, size - done))
                                if not chunk:
                                    raise http.client.IncompleteRead(b"", size - done)
                                limiter.consume(len(chunk))
                                if output.write(chunk) != len(chunk):
                                    raise OSError("A partial file write could not be completed.")
                                done += len(chunk)
                                record(index, done)
                                if time.monotonic() - checkpoint_time >= 0.5:
                                    os.fsync(output.fileno())
                                    record(index, done, True)
                                    checkpoint_time = time.monotonic()
                            # With no Content-Length, verify that the response did not exceed its range.
                            if response.read1(1):
                                raise RangeRejected("A range response contained extra bytes.")
                        finally:
                            os.fsync(output.fileno())
                            record(index, done, True)
                return
            except urllib.error.HTTPError as exc:
                status = exc.code
                retry_after = exc.headers.get("Retry-After", "")
                exc.close()
                if status in {200, 412, 416}:
                    raise RangeRejected("The server no longer accepts the saved file ranges.") from exc
                if status not in RETRY_HTTP or attempt == manager.retries:
                    raise DownloadError(f"A file part failed with HTTP {status}.") from exc
                delay = min(60, int(retry_after)) if retry_after.isdecimal() else manager.retry_delay * 2 ** attempt
            except (urllib.error.URLError, ConnectionError, TimeoutError, http.client.HTTPException) as exc:
                if attempt == manager.retries:
                    raise DownloadError(f"A file part failed after {attempt + 1} attempts: {exc}") from exc
                delay = manager.retry_delay * 2 ** attempt
            pause_wait(delay, control, abort)

    failure = None
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="http-part") as pool:
        futures = [pool.submit(segment, i) for i in range(len(plan["segments"]))]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as exc:
                if failure is None or (isinstance(failure, Interrupted) and not isinstance(exc, Interrupted)):
                    failure = exc
                abort.set()
    manager._check(control)
    if isinstance(failure, RangeRejected):
        clear_checkpoint(job.partial)
        with Path(job.partial).open("wb"):
            pass
        manager._update(job_id, downloaded=0, connection_mode="single", connections=1, speed=0,
                        etag="", modified="", effective_url="",
                        acceleration_note="The server rejected parallel ranges; restarted safely with one connection.", persist=True)
        return False
    if failure is not None:
        raise failure
    if sum(s["done"] for s in plan["segments"]) != plan["total"]:
        raise DownloadError("Some file parts are unfinished. Resume to retry them.")
    return True
