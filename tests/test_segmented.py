"""Parallel byte-range integration tests against a real, instrumented local server."""
from __future__ import annotations
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from fetchline.engine import DownloadManager
from fetchline.segmented import checkpoint_path, read_checkpoint

DATA = bytes(range(256)) * 32768
CHANGED = b"changed!" * (len(DATA) // 8)


class RangeFixture:
    def __init__(self, *, mode="normal", rate=0, shared_rate=0, body=DATA):
        self.mode, self.rate, self.shared_rate, self.body = mode, rate, shared_rate, body
        self.changed = False
        self.lock = threading.Lock()
        self.requests = []
        self.active = self.max_active = 0
        self.failed_once = False
        self.next_slot = 0
        fixture = self
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def log_message(self, *_): pass
            def do_HEAD(self): self.serve(head=True)
            def do_GET(self):
                try: self.serve(head=False)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError): pass
            def serve(self, head):
                requested = self.headers.get("Range", "")
                with fixture.lock:
                    fixture.requests.append({"method": "HEAD" if head else "GET", "range": requested,
                                             "if_range": self.headers.get("If-Range")})
                if head and fixture.mode == "no-head":
                    self.send_error(405); return
                data = CHANGED if fixture.changed else fixture.body
                token = '"version-2"' if fixture.changed else '"version-1"'
                start, end = 0, len(data)-1
                match = re.fullmatch(r"bytes=(\d+)-(\d*)", requested)
                ranged = bool(match)
                probe = requested == "bytes=0-0"
                if ranged:
                    start = int(match[1]); end = int(match[2]) if match[2] else len(data)-1
                    if start >= len(data):
                        self.send_response(416);self.send_header("Content-Range",f"bytes */{len(data)}");self.send_header("Content-Length","0");self.end_headers();return
                if fixture.mode == "ignore" or (fixture.mode == "ignore-parts" and ranged and not probe): ranged=False
                if fixture.mode == "change-parts" and ranged and not probe:
                    fixture.changed=True; data=CHANGED; token='"version-2"'; ranged=False
                if ranged and self.headers.get("If-Range") not in {None,token,"Wed, 21 Oct 2015 07:28:00 GMT"}: ranged=False
                if not ranged:start,end=0,len(data)-1
                is_part=ranged and not probe
                if is_part and fixture.mode == "retry":
                    with fixture.lock:
                        fail = not fixture.failed_once
                        fixture.failed_once=True
                    if fail:
                        self.send_response(503);self.send_header("Content-Length","0");self.send_header("Retry-After","0");self.end_headers();return
                disconnect=False
                if is_part and fixture.mode == "disconnect":
                    with fixture.lock:
                        disconnect=not fixture.failed_once;fixture.failed_once=True
                self.send_response(206 if ranged else 200)
                self.send_header("Content-Type","application/octet-stream")
                if fixture.mode == "date": self.send_header("Last-Modified","Wed, 21 Oct 2015 07:28:00 GMT")
                elif fixture.mode != "no-validator": self.send_header("ETag",'W/'+token if fixture.mode=="weak" else token)
                if ranged:
                    announced_start=start+1 if fixture.mode=="bad-parts" and not probe else start
                    self.send_header("Content-Range",f"bytes {announced_start}-{end}/{len(data)}")
                self.send_header("Content-Length",str(end-start+1));self.end_headers()
                if head:return
                with fixture.lock:
                    if not probe:fixture.active+=1;fixture.max_active=max(fixture.max_active,fixture.active)
                try:
                    final=end+1
                    if disconnect:final=min(final,start+128*1024);self.close_connection=True
                    for offset in range(start,final,32768):
                        chunk=data[offset:min(offset+32768,final)]
                        if fixture.shared_rate:
                            with fixture.lock:
                                fixture.next_slot=max(time.monotonic(),fixture.next_slot)+len(chunk)/fixture.shared_rate
                                due=fixture.next_slot
                            time.sleep(max(0,due-time.monotonic()))
                        elif fixture.rate:time.sleep(len(chunk)/fixture.rate)
                        else:time.sleep(.001 if not probe else 0)
                        self.wfile.write(chunk);self.wfile.flush()
                finally:
                    with fixture.lock:
                        if not probe:fixture.active-=1
        self.server=ThreadingHTTPServer(("127.0.0.1",0),Handler);self.server.daemon_threads=True
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.url=f"http://127.0.0.1:{self.server.server_port}/payload.bin"
    def close(self):self.server.shutdown();self.server.server_close();self.thread.join()


class SegmentTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix="enforcer-ranges-");self.folder=Path(self.temp.name)
        self.db=self.folder/"queue.sqlite3";self.manager=DownloadManager(self.db,timeout=2,retry_delay=.01)
        self.fixture=RangeFixture()
    def tearDown(self):self.manager.shutdown();self.fixture.close();self.temp.cleanup()
    def wait(self,job,predicate=None,timeout=15):
        predicate=predicate or (lambda j:j.state in {"Completed","Error"});deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            live=self.manager.get(job.id)
            if predicate(live):return live
            time.sleep(.005)
        self.fail(f"Timed out: {self.manager.get(job.id)}")
    def complete(self,job,expected=DATA):
        live=self.wait(job);self.assertEqual(live.state,"Completed",live.error)
        self.assertEqual(Path(live.destination).read_bytes(),expected)
        self.assertEqual(live.sha256,hashlib.sha256(expected).hexdigest())
        self.assertFalse(checkpoint_path(live.partial).exists());return live
    def add(self):return self.manager.add(self.fixture.url,self.folder,"test.bin")
    def pause(self):
        self.fixture.rate=1024*1024
        job=self.add();self.wait(job,lambda j:j.downloaded>256*1024)
        self.manager.stop(job.id);paused=self.wait(job,lambda j:j.state=="Paused")
        deadline=time.monotonic()+3
        while job.id in self.manager.threads and time.monotonic()<deadline:time.sleep(.005)
        return paused
    def test_four_connections_fetch_disjoint_ranges_and_verify(self):
        live=self.complete(self.add());self.assertEqual(live.connections,4);self.assertEqual(live.connection_mode,"segmented")
        self.assertGreaterEqual(self.fixture.max_active,2)
        ranges=[r["range"] for r in self.fixture.requests if r["method"]=="GET" and r["range"]!="bytes=0-0"]
        self.assertEqual(len(ranges),4)
    def test_one_connection_setting_disables_probes_and_acceleration(self):
        self.manager.configure(concurrency=2,limit_kib=0,http_connections=1,folder=str(self.folder))
        live=self.complete(self.add());self.assertEqual(live.connections,1);self.assertEqual(len(self.fixture.requests),1)
    def test_eight_connections_are_supported(self):
        self.manager.configure(concurrency=2,limit_kib=0,http_connections=8,folder=str(self.folder))
        live=self.complete(self.add());self.assertEqual(live.connections,8)
    def test_small_file_uses_one_connection(self):
        self.fixture.body=b"small file";live=self.complete(self.add(),b"small file");self.assertEqual(live.connections,1)
    def test_missing_and_weak_validators_fall_back_safely(self):
        for mode in ("no-validator","weak"):
            self.fixture.mode=mode;live=self.complete(self.add());self.assertEqual(live.connection_mode,"single")
    def test_strong_last_modified_date_supports_parallel_ranges(self):
        self.fixture.mode="date";live=self.complete(self.add());self.assertEqual(live.connections,4)
    def test_server_ignoring_ranges_falls_back(self):
        self.fixture.mode="ignore";live=self.complete(self.add());self.assertEqual(live.connection_mode,"single")
    def test_server_without_head_falls_back(self):
        self.fixture.mode="no-head";live=self.complete(self.add());self.assertEqual(live.connection_mode,"single")
    def test_server_rejecting_only_part_requests_falls_back_without_mixing(self):
        self.fixture.mode="ignore-parts";live=self.complete(self.add());self.assertEqual(live.connection_mode,"single")
    def test_wrong_part_range_falls_back_without_mixing(self):
        self.fixture.mode="bad-parts";live=self.complete(self.add());self.assertEqual(live.connection_mode,"single")
    def test_changed_remote_version_restarts_as_a_single_stream(self):
        self.fixture.mode="change-parts";live=self.complete(self.add(),CHANGED);self.assertEqual(live.connection_mode,"single")
    def test_transient_part_error_retries(self):
        self.fixture.mode="retry";self.complete(self.add());self.assertTrue(self.fixture.failed_once)
    def test_dropped_part_resumes_from_its_saved_prefix(self):
        self.fixture.mode="disconnect";self.complete(self.add())
        self.assertGreater(len([r for r in self.fixture.requests if r['range'] and r['range']!='bytes=0-0']),4)
    def test_pause_restart_resumes_durable_parts_not_preallocated_length(self):
        paused=self.pause();plan=read_checkpoint(paused.partial)
        self.assertIsNotNone(plan);self.assertEqual(Path(paused.partial).stat().st_size,len(DATA))
        self.assertLess(paused.downloaded,len(DATA));self.assertGreater(sum(s['done'] for s in plan['segments']),0)
        self.manager.shutdown();self.manager=DownloadManager(self.db,timeout=2,retry_delay=.01)
        recovered=self.manager.get(paused.id);self.assertEqual(recovered.downloaded,sum(s['done'] for s in plan['segments']))
        self.fixture.rate=0;self.manager.resume(paused.id);self.complete(paused)
    def test_remote_version_changed_while_paused_discards_old_parts(self):
        paused=self.pause();self.fixture.changed=True;self.fixture.rate=0;self.manager.resume(paused.id);self.complete(paused,CHANGED)
    def test_corrupt_checkpoint_restarts_safely(self):
        paused=self.pause();checkpoint_path(paused.partial).write_text('{bad json')
        self.fixture.rate=0;self.manager.resume(paused.id);self.complete(paused)
    def test_checkpoint_reset_failure_cannot_reuse_old_prefixes(self):
        paused=self.pause()
        with Path(paused.partial).open("r+b") as output:output.truncate(128)
        self.fixture.rate=0
        with patch("fetchline.segmented.write_checkpoint",side_effect=OSError("simulated checkpoint write failure")):
            self.manager.resume(paused.id);live=self.wait(paused)
            self.assertEqual(live.state,"Error")
        self.assertFalse(checkpoint_path(paused.partial).exists())
        while paused.id in self.manager.threads:time.sleep(.005)
        self.manager.resume(paused.id);self.complete(paused)
    def test_checksum_failure_never_publishes_preallocated_file(self):
        job=self.manager.add(self.fixture.url,self.folder,"bad.bin",expected_sha256="0"*64)
        live=self.wait(job);self.assertEqual(live.state,"Error");self.assertFalse(Path(live.destination).exists())
    def test_removal_deletes_only_owned_partial_and_checkpoint(self):
        paused=self.pause();other=self.folder/'keep.txt';other.write_text('keep')
        self.manager.remove(paused.id);self.assertFalse(Path(paused.partial).exists());self.assertFalse(checkpoint_path(paused.partial).exists());self.assertEqual(other.read_text(),'keep')
    def test_aggregate_rate_limit_and_pause_are_shared_by_all_parts(self):
        self.manager.configure(concurrency=2,limit_kib=256,folder=str(self.folder));start=time.monotonic();job=self.add()
        live=self.wait(job,lambda j:j.downloaded>=256*1024)
        elapsed=time.monotonic()-start;self.assertGreater(elapsed,.8)
        self.assertLessEqual(live.downloaded,256*1024*elapsed+65536)
        self.manager.stop(job.id);self.wait(job,lambda j:j.state=='Paused')
    def test_abrupt_exit_recovers_only_checkpointed_prefixes(self):
        self.fixture.rate=1024*1024;child_db=self.folder/'child.sqlite3';marker=self.folder/'child.json'
        script=("from pathlib import Path\nimport os,sys,json,time\nfrom fetchline.engine import DownloadManager\n"
          "from fetchline.segmented import read_checkpoint\nm=DownloadManager(Path(sys.argv[1]),timeout=2)\n"
          "j=m.add(sys.argv[3],sys.argv[2],'crash.bin')\np=None\n"
          "while not p or sum(s['done'] for s in p['segments'])<131072:\n time.sleep(.01)\n p=read_checkpoint(j.partial)\n"
          "Path(sys.argv[4]).write_text(json.dumps({'id':j.id}))\nos._exit(0)\n")
        subprocess.run([sys.executable,'-c',script,str(child_db),str(self.folder),self.fixture.url,str(marker)],check=True,timeout=12)
        child=DownloadManager(child_db,timeout=2,retry_delay=.01)
        try:
            job=child.get(json.loads(marker.read_text())['id']);plan=read_checkpoint(job.partial)
            self.assertEqual(job.downloaded,sum(s['done'] for s in plan['segments']));self.assertLess(job.downloaded,len(DATA))
            self.fixture.rate=0;child.resume(job.id);deadline=time.monotonic()+12
            while child.get(job.id).state not in {'Completed','Error'} and time.monotonic()<deadline:time.sleep(.01)
            live=child.get(job.id);self.assertEqual(live.state,'Completed',live.error);self.assertEqual(Path(live.destination).read_bytes(),DATA)
        finally:child.shutdown()


if __name__=='__main__':unittest.main(verbosity=2)
