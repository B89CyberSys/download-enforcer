"""Source entry point and executable entry point."""
from __future__ import annotations

import argparse
import ctypes
import json
import os
from pathlib import Path
import sys
import tempfile
import traceback

from fetchline.engine import DownloadManager
from fetchline import APP_NAME, __version__


class InstanceLock:
    def __init__(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        self.file = (directory / "instance.lock").open("a+b")
        self.file.seek(0, 2)
        if self.file.tell() == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError("Download Enforcer is already running. Check its window or system tray.")

    def close(self):
        self.file.close()


def main():
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--data-dir", type=Path, help="Use a separate queue/settings directory")
    parser.add_argument("--self-test", type=Path, metavar="REPORT", help="Run an isolated native GUI startup check")
    parser.add_argument("--test-torrent", type=Path, help="With --self-test: transfer a generated torrent using loopback-only peers")
    parser.add_argument("--test-http", help="With --self-test: transfer a generated file from a loopback HTTP server")
    args = parser.parse_args()
    if args.test_torrent and not args.self_test:
        parser.error("--test-torrent requires --self-test")
    if args.test_http:
        from urllib.parse import urlsplit
        target = urlsplit(args.test_http)
        if not args.self_test or target.scheme != "http" or target.hostname != "127.0.0.1":
            parser.error("--test-http requires --self-test and a loopback HTTP URL")
    if args.self_test:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QMessageBox
    from fetchline.qt_gui import EnforcerWindow
    from fetchline.torrents import library
    app = QApplication([sys.argv[0]])
    if args.self_test and os.name == "nt":
        from PySide6.QtGui import QFontDatabase
        for name in ("segoeui.ttf", "segoeuib.ttf", "seguisb.ttf"):
            QFontDatabase.addApplicationFont(str(Path(os.getenv("WINDIR", "C:/Windows")) / "Fonts" / name))
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)
    if args.self_test:
        args.self_test.parent.mkdir(parents=True, exist_ok=True)
    temp = tempfile.TemporaryDirectory(prefix="fetchline-smoke-", dir=args.self_test.parent) if args.self_test else None
    data_dir = Path(temp.name) if temp else (args.data_dir or Path(os.getenv("LOCALAPPDATA", str(Path.home() / ".local"))) / "Fetchline")
    lock = None
    try:
        lock = InstanceLock(data_dir)
        manager = DownloadManager(data_dir / "queue.sqlite3")
        window = EnforcerWindow(manager, enable_tray=not bool(args.self_test))
        if args.self_test:
            window.show(); app.processEvents()
            for theme in ("dark", "light"):
                window.theme = theme; window.apply_theme(); app.processEvents()
                window.grab().save(str(args.self_test.parent / f"enforcer-{theme}.png"))
            for torrent in (False, True):
                dialog = window.add_dialog(torrent=torrent); app.processEvents(); dialog.reject()
            dialog = window.settings_dialog(); app.processEvents(); dialog.reject()
            window.timer.stop()
            report = {"ok": True, "version": __version__, "torrent_engine": library().__version__,
                      "native_gui": "Qt created and rendered in dark/light themes", "dialogs": ["new download", "torrent", "settings"],
                      "queue": "opened", "network_requests": 0}
            if args.test_http:
                import time
                job = manager.add(args.test_http, temp.name, "packaged-http.bin")
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline:
                    app.processEvents()
                    live = manager.get(job.id)
                    if live.state in {"Completed", "Error"}:
                        break
                    time.sleep(.01)
                if live.state != "Completed":
                    raise RuntimeError(f"Packaged HTTP transfer failed: {live.state}: {live.error}")
                report["http_transfer"] = {"state": live.state, "bytes": live.downloaded, "sha256": live.sha256,
                                           "mode": live.connection_mode, "connections": live.connections}
            if args.test_torrent:
                import time
                import hashlib
                manager.torrent_settings = {"listen_interfaces": "127.0.0.1:0", "outgoing_interfaces": "127.0.0.1",
                    "enable_dht": False, "enable_lsd": False, "enable_upnp": False, "enable_natpmp": False,
                    "enable_incoming_utp": False, "enable_outgoing_utp": False, "allow_multiple_connections_per_ip": True}
                job = manager.add_torrent(str(args.test_torrent.resolve()), temp.name)
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline:
                    app.processEvents()
                    live = manager.get(job.id)
                    if live.state in {"Completed", "Error"}:
                        break
                    time.sleep(0.03)
                if live.state != "Completed":
                    raise RuntimeError(f"Packaged torrent transfer failed: {live.state}: {live.error}")
                files = {str(p.relative_to(live.destination)): hashlib.sha256(p.read_bytes()).hexdigest()
                         for p in Path(live.destination).rglob("*") if p.is_file()}
                report["torrent_transfer"] = {"state": live.state, "bytes": live.downloaded, "files": files, "discovery": "loopback only"}
            args.self_test.parent.mkdir(parents=True, exist_ok=True)
            args.self_test.write_text(json.dumps(report, indent=2), encoding="utf-8")
            manager.shutdown()
            window.hide()
        else:
            app.aboutToQuit.connect(lambda: manager.shutdown() if not manager.closing else None)
            window.show()
            app.exec()
    except Exception as exc:
        if args.self_test:
            args.self_test.parent.mkdir(parents=True, exist_ok=True)
            args.self_test.write_text(json.dumps({"ok": False, "error": traceback.format_exc()}, indent=2), encoding="utf-8")
        else:
            QMessageBox.critical(None, APP_NAME, str(exc))
        return 1
    finally:
        if lock:
            lock.close()
        if temp:
            temp.cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main())
