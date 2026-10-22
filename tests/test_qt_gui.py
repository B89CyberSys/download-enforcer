"""Exercise real Qt forms, native rendering, theme persistence and tray close behavior."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from PySide6.QtWidgets import QApplication, QPlainTextEdit
from PySide6.QtGui import QFontDatabase, QCloseEvent
from fetchline.engine import DownloadManager
from fetchline.qt_gui import EnforcerWindow
from run import InstanceLock
from test_engine import BODY, Fixture
from test_torrents import Swarm, LOCAL
from test_segmented import RangeFixture, DATA


class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)
        if os.name=="nt":
            for name in ("segoeui.ttf","segoeuib.ttf","seguisb.ttf"):
                QFontDatabase.addApplicationFont(str(Path(os.getenv("WINDIR","C:/Windows"))/"Fonts"/name))

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix="enforcer-ui-")
        self.folder=Path(self.temp.name)
        self.db=self.folder/"queue.sqlite3"
        self.manager=DownloadManager(self.db)
        self.manager.torrent_settings=LOCAL
        self.manager.configure(concurrency=2,limit_kib=0,folder=str(self.folder))
        self.window=EnforcerWindow(self.manager,enable_tray=False)
        self.window.show();self.app.processEvents()

    def tearDown(self):
        self.window.timer.stop();self.window.tray.hide();self.window.hide()
        self.manager.shutdown();self.window.deleteLater();self.app.processEvents();self.temp.cleanup()

    def wait_job(self,job,timeout=30):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            self.app.processEvents();self.window.refresh()
            live=self.manager.get(job.id)
            if live.state in {"Completed","Error"}:return live
            time.sleep(.01)
        self.fail("UI download did not finish")

    def test_add_direct_download_progress_details_and_filters(self):
        fixture=Fixture()
        try:
            d=self.window.add_dialog();d.source_edit.setText(fixture.url+"/file");d.name_edit.setText("from-ui.bin");d.start_button.click()
            self.assertIsNotNone(d.job)
            live=self.wait_job(d.job);self.assertEqual(live.state,"Completed",live.error)
            self.assertEqual(Path(live.destination).read_bytes(),BODY)
            self.window.select(live.id);self.assertIn(live.destination,self.window.destination_button.text())
            detail=self.window.details_dialog(live);self.app.processEvents()
            self.assertIn(live.sha256,detail.findChild(QPlainTextEdit).toPlainText());detail.reject()
            self.window.set_filter("Paused");self.assertEqual(self.window.rows,{})
            self.window.set_filter("Completed");self.assertEqual(len(self.window.rows),1)
            self.window.search.setText("not a matching filename");self.assertEqual(self.window.rows,{})
        finally:fixture.close()

    def test_torrent_form_downloads_generated_torrent(self):
        swarm=Swarm(self.folder/"swarm")
        try:
            d=self.window.add_dialog(torrent=True);d.mode.setCurrentIndex(2);d.source_edit.setText(str(swarm.file));d.start_button.click()
            self.assertIsNotNone(d.job)
            live=self.wait_job(d.job,timeout=45);self.assertEqual(live.state,"Completed",live.error)
            self.assertEqual((Path(live.destination)/"test-bundle"/"payload.bin").read_bytes(),BODY)
            self.window.set_filter("Torrents");self.assertIn(live.id,self.window.rows)
        finally:swarm.close()

    def test_theme_button_changes_theme_and_persists(self):
        self.assertEqual(self.window.theme,"dark")
        self.window.theme_button.click();self.app.processEvents()
        self.assertEqual(self.window.theme,"light")
        self.assertEqual(self.manager.preference("theme"),"light")
        self.assertEqual(self.window.theme_button.text(),"Dark mode")

    def test_parallel_download_through_form_and_connection_setting_persists(self):
        fixture=RangeFixture()
        try:
            d=self.window.add_dialog();d.source_edit.setText(fixture.url);d.start_button.click()
            live=self.wait_job(d.job);self.assertEqual(live.state,"Completed",live.error)
            self.assertEqual(live.connection_mode,"segmented");self.assertEqual(live.connections,4)
            self.assertEqual(Path(live.destination).read_bytes(),DATA)
            detail=self.window.details_dialog(live)
            self.assertIn("4 connections",detail.findChild(QPlainTextEdit).toPlainText());detail.reject()
            settings=self.window.settings_dialog();settings.inputs["http_connections"].setValue(1)
            settings.save()
            self.assertEqual(self.manager.settings()["http_connections"],1)
            self.assertEqual(self.manager.store.setting("http_connections",0),1)
        finally:fixture.close()

    def test_close_to_tray_keeps_manager_running(self):
        self.window.tray_enabled=True
        with patch.object(self.window.tray,"isVisible",return_value=True),patch.object(self.window.tray,"showMessage"):
            event=QCloseEvent();self.window.closeEvent(event)
            self.assertFalse(event.isAccepted());self.assertFalse(self.window.isVisible())
            self.assertFalse(self.window.closing);self.assertFalse(self.manager.closing)
        self.window.show_from_tray();self.app.processEvents();self.assertTrue(self.window.isVisible())

    def test_theme_and_settings_controls_fit_at_small_window_size(self):
        self.window.resize(800,540);self.app.processEvents()
        area=self.window.rect()
        for control in (self.window.theme_button,self.window.settings_button,self.window.new_button,self.window.torrent_button,self.window.footer):
            top_left=control.mapTo(self.window,control.rect().topLeft())
            bottom_right=control.mapTo(self.window,control.rect().bottomRight())
            self.assertTrue(area.contains(top_left),control.objectName())
            self.assertTrue(area.contains(bottom_right),control.objectName())

    def test_single_instance_lock_blocks_another_process(self):
        lock=InstanceLock(self.folder)
        try:
            script="from pathlib import Path\nfrom run import InstanceLock\nimport sys\ntry: l=InstanceLock(Path(sys.argv[1]))\nexcept RuntimeError: sys.exit(0)\nelse: l.close();sys.exit(1)\n"
            result=subprocess.run([sys.executable,"-c",script,str(self.folder)],timeout=10)
            self.assertEqual(result.returncode,0)
        finally:lock.close()


if __name__=="__main__":unittest.main(verbosity=2)
