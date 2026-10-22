"""Integration tests use a real, loopback-only HTTP server and real temporary files."""
from __future__ import annotations

from collections import Counter
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from fetchline import __version__
from fetchline.engine import DownloadManager, Job, download_filename, filename_from_url, normalize_url, safe_filename, validate_download_link

BODY = bytes(range(256)) * 8192
OTHER = b"changed!" * (len(BODY) // 8)
HTML = b"<!DOCTYPE html><html><body>This is a webpage, not a video.</body></html>"
MODIFIED = "Wed, 21 Oct 2015 07:28:00 GMT"


class Fixture:
    def __init__(self):
        self.requests = []
        self.counts = Counter()
        self.changed = False
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            def do_GET(self):
                try:
                    self.serve()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

            def serve(self):
                route = self.path.split("?")[0]
                fixture.counts[route] += 1
                fixture.requests.append((route, dict(self.headers)))
                if route == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/file")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if route == "/404":
                    self.send_error(404)
                    return
                if route == "/retry" and fixture.counts[route] < 3:
                    self.send_response(503)
                    self.send_header("Retry-After", "0")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if route == "/empty":
                    self.send_response(200)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                data = OTHER if fixture.changed else BODY
                if route == "/webpage":
                    data = HTML
                etag = '"v2"' if fixture.changed else '"v1"'
                offset = 0
                requested = self.headers.get("Range", "")
                use_range = bool(requested and self.headers.get("If-Range") in {etag, MODIFIED})
                if route == "/ignore-range":
                    use_range = False
                if route == "/416" and requested:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{len(data)}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if route == "/changed-206" and requested:
                    use_range = True
                if use_range:
                    offset = int(requested.removeprefix("bytes=").removesuffix("-"))
                self.send_response(206 if use_range else 200)
                if route == "/webpage":
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                if route != "/no-validator":
                    if route == "/date-validator":
                        self.send_header("Last-Modified", MODIFIED)
                    else:
                        self.send_header("ETag", etag)
                if use_range:
                    start = offset + 1 if route == "/bad-range" else offset
                    self.send_header("Content-Range", f"bytes {start}-{len(data)-1}/{len(data)}")
                if route == "/chunked":
                    self.send_header("Transfer-Encoding", "chunked")
                elif route == "/unknown":
                    self.send_header("Connection", "close")
                    self.close_connection = True
                else:
                    self.send_header("Content-Length", str(len(data) - offset))
                self.end_headers()
                stop = len(data)
                if route == "/disconnect" and fixture.counts[route] == 1:
                    stop = min(stop, offset + 96 * 1024)
                    self.close_connection = True
                for start in range(offset, stop, 8192):
                    chunk = data[start:min(start + 8192, stop)]
                    if route == "/chunked":
                        self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                    else:
                        self.wfile.write(chunk)
                    self.wfile.flush()
                    if route in {"/slow", "/ignore-range", "/bad-range", "/changed", "/changed-206",
                                 "/no-validator", "/date-validator", "/416"}:
                        time.sleep(0.002)
                if route == "/chunked":
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class EngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = Fixture()

    @classmethod
    def tearDownClass(cls):
        cls.fixture.close()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fetchline-test-")
        self.folder = Path(self.temp.name)
        self.db = self.folder / "queue.sqlite3"
        self.manager = DownloadManager(self.db, concurrency=1, timeout=2, retry_delay=0.01)
        self.fixture.requests.clear()
        self.fixture.counts.clear()
        self.fixture.changed = False

    def tearDown(self):
        self.manager.shutdown()
        self.temp.cleanup()

    def add(self, route="/file", **kwargs):
        return self.manager.add(self.fixture.url + route, self.folder, "example.bin", **kwargs)

    def wait(self, job, predicate=None, timeout=8):
        predicate = predicate or (lambda j: j.state in {"Completed", "Error"})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            live = self.manager.get(job.id)
            if predicate(live):
                return live
            time.sleep(0.005)
        self.fail(f"Timed out waiting for {job.id}: {self.manager.get(job.id)}")

    def complete(self, job, expected=BODY):
        live = self.wait(job)
        self.assertEqual(live.state, "Completed", live.error)
        self.assertEqual(Path(live.destination).read_bytes(), expected)
        self.assertEqual(live.sha256, hashlib.sha256(expected).hexdigest())
        self.assertFalse(Path(live.partial).exists())
        return live

    def paused(self, route="/slow"):
        job = self.add(route)
        self.wait(job, lambda j: j.downloaded >= 32768)
        self.manager.stop(job.id)
        live = self.wait(job, lambda j: j.state == "Paused")
        self.assertGreater(Path(live.partial).stat().st_size, 0)
        self.assertLess(Path(live.partial).stat().st_size, len(BODY))
        # The state is committed immediately before the worker leaves; wait for slot release.
        deadline = time.monotonic() + 2
        while job.id in self.manager.threads and time.monotonic() < deadline:
            time.sleep(0.005)
        return live

    def test_full_download_and_sha256(self):
        self.complete(self.add())

    def test_youtube_page_rejected_without_creating_a_job_or_file(self):
        with self.assertRaisesRegex(ValueError, "YouTube video downloading is not supported"):
            self.manager.add("https://www.youtube.com/watch?v=example", self.folder, "video")
        self.assertEqual(self.manager.snapshot(), [])
        self.assertEqual(list(self.folder.glob("*.part")), [])

    def test_html_response_not_published_as_downloaded_video(self):
        job = self.manager.add(self.fixture.url + "/webpage", self.folder, "video.mp4")
        live = self.wait(job)
        self.assertEqual(live.state, "Error")
        self.assertIn("webpage", live.error)
        self.assertEqual(Path(live.partial).stat().st_size, 0)
        self.assertFalse(Path(live.destination).exists())

    def test_intentional_html_download_still_supported(self):
        job = self.manager.add(self.fixture.url + "/webpage", self.folder, "page.html")
        self.complete(job, HTML)

    def test_archive_extension_kept_with_dotted_custom_name(self):
        job = self.manager.add(self.fixture.url + "/archive.zip", self.folder, "Ghidra 12.14")
        live = self.complete(job)
        self.assertEqual(Path(live.destination).name, "Ghidra 12.14.zip")

    def test_supplied_sha256_verified(self):
        live = self.complete(self.add(expected_sha256=hashlib.sha256(BODY).hexdigest()))
        self.assertIn("verified", live.note)

    def test_checksum_mismatch_never_publishes_file(self):
        job = self.add(expected_sha256="0" * 64)
        live = self.wait(job)
        self.assertEqual(live.state, "Error")
        self.assertIn("mismatch", live.error)
        self.assertFalse(Path(live.destination).exists())
        self.assertTrue(Path(live.partial).exists())

    def test_empty_file(self):
        self.complete(self.add("/empty"), b"")

    def test_unknown_length(self):
        self.complete(self.add("/unknown"))

    def test_chunked_transfer(self):
        self.complete(self.add("/chunked"))

    def test_redirect(self):
        live = self.complete(self.add("/redirect"))
        self.assertTrue(live.effective_url.endswith("/file"))

    def test_transient_http_503_is_retried(self):
        self.complete(self.add("/retry"))
        self.assertEqual(self.fixture.counts["/retry"], 3)

    def test_permanent_404_is_not_retried(self):
        live = self.wait(self.add("/404"))
        self.assertEqual(live.state, "Error")
        self.assertIn("404", live.error)
        self.assertEqual(self.fixture.counts["/404"], 1)

    def test_disconnect_retries_and_resumes_without_corruption(self):
        self.complete(self.add("/disconnect"))
        self.assertTrue(any(headers.get("Range") for _, headers in self.fixture.requests))

    def test_pause_and_resume_uses_if_range(self):
        job = self.paused()
        offset = Path(job.partial).stat().st_size
        self.manager.resume(job.id)
        self.complete(job)
        self.assertTrue(any(h.get("Range") == f"bytes={offset}-" and h.get("If-Range") == '"v1"'
                            for _, h in self.fixture.requests))

    def test_no_validator_restarts_without_range(self):
        job = self.paused("/no-validator")
        self.manager.resume(job.id)
        self.complete(job)
        self.assertFalse(any(h.get("Range") for _, h in self.fixture.requests))

    def test_last_modified_resume(self):
        job = self.paused("/date-validator")
        self.manager.resume(job.id)
        self.complete(job)
        self.assertTrue(any(h.get("If-Range") == MODIFIED for _, h in self.fixture.requests))

    def test_server_ignores_range_restarts_safely(self):
        job = self.paused("/ignore-range")
        self.manager.resume(job.id)
        self.complete(job)

    def test_changed_etag_200_replaces_old_bytes(self):
        job = self.paused("/changed")
        self.fixture.changed = True
        self.manager.resume(job.id)
        self.complete(job, OTHER)

    def test_changed_etag_206_rejected_before_append(self):
        job = self.paused("/changed-206")
        before = Path(job.partial).read_bytes()
        self.fixture.changed = True
        self.manager.resume(job.id)
        live = self.wait(job)
        self.assertEqual(live.state, "Error")
        self.assertIn("changed", live.error)
        self.assertEqual(Path(job.partial).read_bytes(), before)

    def test_wrong_content_range_rejected_before_append(self):
        job = self.paused("/bad-range")
        before = Path(job.partial).read_bytes()
        self.manager.resume(job.id)
        live = self.wait(job)
        self.assertEqual(live.state, "Error")
        self.assertIn("wrong resume range", live.error)
        self.assertEqual(Path(job.partial).read_bytes(), before)

    def test_416_falls_back_to_full_download(self):
        job = self.paused("/416")
        self.manager.resume(job.id)
        self.complete(job)
        self.assertEqual(self.fixture.counts["/416"], 3)

    def test_recovery_after_manager_restart(self):
        job = self.paused()
        self.manager.shutdown()
        self.manager = DownloadManager(self.db, timeout=2, retry_delay=0.01)
        self.assertEqual(self.manager.get(job.id).state, "Paused")
        self.manager.resume(job.id)
        self.complete(job)

    def test_upgrade_backup_and_queue_guard_recover_only_unexpected_loss(self):
        legacy_db = self.folder / "legacy.sqlite3"
        first = Job("first", self.fixture.url + "/file", str(self.folder / "first.bin"),
                    str(self.folder / ".first.part"), state="Completed", created=1)
        second = Job("second", self.fixture.url + "/file", str(self.folder / "second.bin"),
                     str(self.folder / ".second.part"), state="Completed", created=2)
        connection = sqlite3.connect(legacy_db)
        connection.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        connection.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.executemany(
            "INSERT INTO jobs VALUES (?, ?)",
            [(job.id, json.dumps(job.__dict__)) for job in (first, second)],
        )
        connection.commit()
        connection.close()

        upgraded = DownloadManager(legacy_db, autostart=False)
        self.assertEqual({job.id for job in upgraded.snapshot()}, {"first", "second"})
        self.assertEqual(upgraded.store.recovered_jobs, 0)
        backup = self.folder / "backups" / f"queue-before-{__version__}.sqlite3"
        self.assertTrue(backup.exists())
        connection = sqlite3.connect(backup)
        try:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 2)
        finally:
            connection.close()
        upgraded.shutdown()

        connection = sqlite3.connect(legacy_db)
        try:
            connection.execute("DELETE FROM jobs WHERE id='second'")
            connection.commit()
        finally:
            connection.close()
        recovered = DownloadManager(legacy_db, autostart=False)
        self.assertEqual({job.id for job in recovered.snapshot()}, {"first", "second"})
        self.assertEqual(recovered.store.recovered_jobs, 1)
        recovered.remove("first")
        recovered.shutdown()

        reopened = DownloadManager(legacy_db, autostart=False)
        try:
            self.assertEqual({job.id for job in reopened.snapshot()}, {"second"})
            self.assertEqual(reopened.store.recovered_jobs, 0)
        finally:
            reopened.shutdown()

    def test_abrupt_process_exit_recovers_actual_partial_size(self):
        child_db = self.folder / "child.sqlite3"
        marker = self.folder / "child.json"
        script = ("from pathlib import Path\nimport json, os, sys, time\n"
                  "from fetchline.engine import DownloadManager\n"
                  "m=DownloadManager(Path(sys.argv[1]), timeout=2)\n"
                  "j=m.add(sys.argv[3], sys.argv[2], 'crash.bin')\n"
                  "while m.get(j.id).downloaded < 131072: time.sleep(.002)\n"
                  "Path(sys.argv[4]).write_text(json.dumps({'id':j.id}))\n"
                  "os._exit(0)\n")
        subprocess.run([sys.executable, "-c", script, str(child_db), str(self.folder),
                        self.fixture.url + "/slow", str(marker)], check=True, timeout=10)
        child = DownloadManager(child_db, timeout=2, retry_delay=0.01)
        try:
            job = child.get(json.loads(marker.read_text())["id"])
            self.assertEqual(job.state, "Paused")
            self.assertEqual(job.downloaded, Path(job.partial).stat().st_size)
            child.resume(job.id)
            deadline = time.monotonic() + 8
            while child.get(job.id).state not in {"Completed", "Error"} and time.monotonic() < deadline:
                time.sleep(0.01)
            live = child.get(job.id)
            self.assertEqual(live.state, "Completed", live.error)
            self.assertEqual(Path(live.destination).read_bytes(), BODY)
        finally:
            child.shutdown()

    def test_existing_file_never_overwritten(self):
        original = self.folder / "example.bin"
        original.write_bytes(b"keep me")
        live = self.complete(self.add())
        self.assertNotEqual(Path(live.destination), original)
        self.assertEqual(original.read_bytes(), b"keep me")

    def test_destination_created_during_transfer_never_overwritten(self):
        job = self.paused()
        original = Path(job.destination)
        original.write_bytes(b"keep me too")
        self.manager.resume(job.id)
        live = self.complete(job)
        self.assertNotEqual(Path(live.destination), original)
        self.assertEqual(original.read_bytes(), b"keep me too")

    def test_queue_concurrency_and_pause_all(self):
        first = self.add("/slow")
        second = self.add("/slow")
        self.assertEqual(self.manager.get(second.id).state, "Queued")
        self.manager.pause_all()
        self.wait(first, lambda j: j.state == "Paused")
        self.assertEqual(self.manager.get(second.id).state, "Paused")
        self.manager.resume_all()
        self.complete(first)
        self.complete(second)

    def test_remove_completed_keeps_download(self):
        job = self.complete(self.add())
        self.manager.remove(job.id)
        self.assertTrue(Path(job.destination).exists())
        self.assertEqual(self.manager.snapshot(), [])

    def test_remove_partial_only_deletes_owned_partial(self):
        job = self.paused()
        other = self.folder / "unrelated.part"
        other.write_bytes(b"keep")
        self.manager.remove(job.id)
        self.assertFalse(Path(job.partial).exists())
        self.assertEqual(other.read_bytes(), b"keep")

    def test_settings_persist(self):
        self.manager.configure(concurrency=3, limit_kib=512, folder=str(self.folder))
        self.manager.shutdown()
        self.manager = DownloadManager(self.db)
        self.assertEqual(self.manager.settings(), {"concurrency": 3, "limit_kib": 512, "upload_limit_kib": 128, "http_connections": 4, "folder": str(self.folder)})

    def test_speed_limit_can_be_paused_promptly(self):
        self.manager.configure(concurrency=1, limit_kib=1, folder=str(self.folder))
        job = self.add("/slow")
        self.wait(job, lambda j: j.downloaded > 0)
        start = time.monotonic()
        self.manager.stop(job.id)
        self.wait(job, lambda j: j.state == "Paused")
        self.assertLess(time.monotonic() - start, 1)


class InputTests(unittest.TestCase):
    def test_custom_names_keep_file_extensions(self):
        for source, chosen, expected in [
                ("https://example.com/archive.zip", "My download", "My download.zip"),
                ("https://example.com/archive.zip", "Ghidra 12.14", "Ghidra 12.14.zip"),
                ("https://example.com/archive.zip", "custom.bin", "custom.bin"),
                ("https://example.com/archive.tar.gz", "Backup", "Backup.tar.gz"),
                ("https://example.com/file.zip", "", "file.zip")]:
            with self.subTest(chosen=chosen):
                self.assertEqual(download_filename(source, chosen), expected)

    def test_youtube_detection_matches_real_video_page_hosts(self):
        for url in ("https://www.youtube.com/watch?v=a", "https://youtu.be/a", "https://m.youtube.com/shorts/a",
                    "https://youtube.com/live/a", "https://www.youtube-nocookie.com/embed/a"):
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, "YouTube"):
                validate_download_link(url)
        for url in ("https://example.com/watch?v=a", "https://youtube.com.example.org/watch?v=a",
                    "https://youtube.com/robots.txt"):
            validate_download_link(url)

    def test_rejects_non_http_links_and_credentials(self):
        for value in ("file:///c:/windows/win.ini", "ftp://example.com/a", "javascript:alert(1)",
                      "http://user:pass@example.com/file", "http://", "http://example.com:99999/a"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_url(value)

    def test_normalizes_unicode_and_fragments(self):
        self.assertEqual(normalize_url(" https://example.com/a b/ü.zip#test "),
                         "https://example.com/a%20b/%C3%BC.zip")

    def test_windows_filename_sanitization(self):
        for value in ("CON.txt", "LPT1", "AUX", "COM¹.exe", "NUL"):
            self.assertTrue(safe_filename(value).startswith("_"))
        self.assertNotIn("/", safe_filename("../test"))
        self.assertEqual(filename_from_url("https://example.com/folder/"), "download.bin")
        self.assertEqual(filename_from_url("https://example.com/test%20file.zip?q=2"), "test file.zip")


if __name__ == "__main__":
    unittest.main(verbosity=2)
