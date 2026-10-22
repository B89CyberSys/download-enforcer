"""Real BitTorrent transfers between local peers, using a local tracker and generated data."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import hashlib
import socket
import struct
import tempfile
import threading
import time
import unittest
from urllib.parse import quote

import libtorrent as lt

from fetchline.engine import DownloadManager
from fetchline.torrents import load_source, session_settings

LOCAL = {"listen_interfaces": "127.0.0.1:0", "outgoing_interfaces": "127.0.0.1",
         "enable_dht": False, "enable_lsd": False, "enable_upnp": False, "enable_natpmp": False,
         "enable_incoming_utp": False, "enable_outgoing_utp": False,
         "allow_multiple_connections_per_ip": True}
DATA = bytes(range(256)) * 8192


class Swarm:
    def __init__(self, root):
        self.root = root
        self.seed_root = root / "seed"
        bundle = self.seed_root / "test-bundle"
        bundle.mkdir(parents=True)
        (bundle / "payload.bin").write_bytes(DATA)
        (bundle / "readme.txt").write_bytes(b"Generated local test data only.\n")
        self.session = lt.session(session_settings(LOCAL))
        deadline = time.monotonic() + 5
        while not self.session.listen_port() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.port = self.session.listen_port()
        swarm = self
        class Tracker(BaseHTTPRequestHandler):
            def do_GET(self):
                response = lt.bencode({"interval": 1, "complete": 1, "incomplete": 0,
                                       "peers": socket.inet_aton("127.0.0.1") + struct.pack("!H", swarm.port)})
                self.send_response(200)
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)
            def log_message(self, *_):
                pass
        self.tracker = ThreadingHTTPServer(("127.0.0.1", 0), Tracker)
        self.thread = threading.Thread(target=self.tracker.serve_forever, daemon=True)
        self.thread.start()
        self.tracker_url = f"http://127.0.0.1:{self.tracker.server_port}/announce"
        storage = lt.file_storage()
        lt.add_files(storage, str(bundle))
        torrent = lt.create_torrent(storage, 32 * 1024, lt.create_torrent.v1_only)
        torrent.add_tracker(self.tracker_url)
        lt.set_piece_hashes(torrent, str(self.seed_root))
        raw = lt.bencode(torrent.generate())
        self.file = root / "generated.torrent"
        self.file.write_bytes(raw)
        self.info = lt.torrent_info(lt.bdecode(raw))
        params = lt.add_torrent_params()
        params.ti = self.info
        params.save_path = str(self.seed_root)
        params.flags &= ~(lt.torrent_flags.auto_managed | lt.torrent_flags.paused)
        params.flags |= lt.torrent_flags.seed_mode
        self.handle = self.session.add_torrent(params)
        self.magnet = lt.make_magnet_uri(self.info) + "&x.pe=127.0.0.1:" + str(self.port)

    def close(self):
        self.session.pause()
        self.session.remove_torrent(self.handle)
        self.tracker.shutdown()
        self.tracker.server_close()
        self.thread.join()


class TorrentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="enforcer-torrent-")
        self.root = Path(self.temp.name)
        self.swarm = Swarm(self.root)
        self.output = self.root / "downloads"
        self.output.mkdir()
        self.db = self.root / "queue.sqlite3"
        self.manager = DownloadManager(self.db)
        self.manager.torrent_settings = LOCAL

    def tearDown(self):
        self.manager.shutdown()
        self.swarm.close()
        self.temp.cleanup()

    def wait(self, job, predicate=None, timeout=35):
        deadline = time.monotonic() + timeout
        predicate = predicate or (lambda j: j.state in {"Completed", "Error"})
        while time.monotonic() < deadline:
            live = self.manager.get(job.id)
            if live.state == "Error":
                self.fail(live.error)
            if predicate(live):
                return live
            time.sleep(0.05)
        self.fail(f"Torrent timed out: {self.manager.get(job.id)}")

    def assert_payload(self, job):
        file = Path(job.destination) / "test-bundle" / "payload.bin"
        self.assertEqual(hashlib.sha256(file.read_bytes()).digest(), hashlib.sha256(DATA).digest())
        self.assertEqual((file.parent / "readme.txt").read_bytes(), b"Generated local test data only.\n")

    def test_torrent_file_download_piece_verification_and_stop(self):
        job = self.manager.add_torrent(str(self.swarm.file), self.output)
        live = self.wait(job)
        self.assertEqual(live.state, "Completed")
        self.assertEqual(live.file_count, 2)
        self.assertEqual(live.downloaded, live.total)
        self.assertFalse(live.seed_after_complete)
        self.assertTrue(Path(live.partial).is_file())
        self.assert_payload(live)

    def test_magnet_fetches_metadata_and_downloads(self):
        job = self.manager.add(self.swarm.magnet, self.output)
        live = self.wait(job)
        self.assertEqual(live.state, "Completed")
        self.assertEqual(live.file_count, 2)
        self.assert_payload(live)

    def test_pause_restart_and_resume_preserves_pieces(self):
        self.manager.configure(concurrency=2, limit_kib=256, upload_limit_kib=16, folder=str(self.output))
        job = self.manager.add_torrent(str(self.swarm.file), self.output)
        self.wait(job, lambda j: j.downloaded >= 65536)
        self.manager.stop(job.id)
        paused = self.wait(job, lambda j: j.state == "Paused")
        self.assertGreater(paused.downloaded, 0)
        self.assertTrue(Path(paused.partial).is_file())
        self.manager.shutdown()
        self.manager = DownloadManager(self.db)
        self.manager.torrent_settings = LOCAL
        recovered = self.manager.get(job.id)
        self.assertEqual(recovered.state, "Paused")
        self.assertEqual(recovered.downloaded, paused.downloaded)
        self.manager.configure(concurrency=2, limit_kib=0, folder=str(self.output))
        self.manager.resume(job.id)
        self.assert_payload(self.wait(job))

    def test_seeding_can_be_stopped_and_restarted(self):
        job = self.manager.add_torrent(str(self.swarm.file), self.output, seed=True)
        seeded = self.wait(job, lambda j: j.state == "Seeding")
        self.assert_payload(seeded)
        self.manager.set_seeding(job.id, False)
        self.wait(job)
        self.manager.set_seeding(job.id, True)
        self.wait(job, lambda j: j.state == "Seeding")
        self.manager.stop(job.id)
        self.wait(job, lambda j: j.state == "Paused")

    def test_duplicate_infohash_rejected(self):
        job = self.manager.add_torrent(str(self.swarm.file), self.output)
        with self.assertRaisesRegex(ValueError, "already in your queue"):
            self.manager.add_torrent(self.swarm.magnet, self.output)

    def test_invalid_source_creates_no_payload_folder(self):
        bad = self.root / "bad.torrent"
        bad.write_bytes(b"not bencoded metadata")
        with self.assertRaises(ValueError):
            self.manager.add_torrent(str(bad), self.output)
        with self.assertRaises((ValueError, RuntimeError)):
            self.manager.add_torrent("magnet:?dn=missinghash", self.output)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_remove_retains_torrent_payload(self):
        job = self.manager.add_torrent(str(self.swarm.file), self.output)
        live = self.wait(job)
        self.manager.remove(job.id)
        self.assert_payload(live)

    def test_missing_peers_can_be_paused_without_metadata(self):
        job = self.manager.add_torrent("magnet:?xt=urn:btih:" + "1" * 40, self.output)
        self.wait(job, lambda j: j.state == "Finding metadata")
        self.manager.stop(job.id)
        self.wait(job, lambda j: j.state == "Paused")


if __name__ == "__main__":
    unittest.main(verbosity=2)
