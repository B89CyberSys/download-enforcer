"""Responsive Qt desktop interface for Download Enforcer."""
from __future__ import annotations
import ctypes
import os
from pathlib import Path
import subprocess
import sys
import threading
from urllib.parse import urlsplit
from PySide6.QtCore import Qt, QTimer, QSize, Signal, QPointF, QRectF
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap, QPolygonF, QShortcut, QKeySequence
from PySide6.QtWidgets import (QApplication,QMainWindow,QWidget,QFrame,QLabel,QPushButton,QToolButton,
 QLineEdit,QVBoxLayout,QHBoxLayout,QScrollArea,QSizePolicy,QProgressBar,QDialog,QFileDialog,
 QComboBox,QCheckBox,QSpinBox,QMessageBox,QPlainTextEdit,QSystemTrayIcon,QMenu,QStackedWidget)
from . import APP_NAME,__version__
from .engine import ACTIVE

COLORS={"dark":dict(bg="#101317",side="#151a20",panel="#1a2028",hover="#232c37",line="#2c3642",text="#eef4fc",muted="#91a0b5",accent="#6ce4d0",ink="#102923",selected="#203832",red="#ff9f9f"),
 "light":dict(bg="#f4f6fa",side="#ffffff",panel="#ffffff",hover="#eaf0f6",line="#dce3ed",text="#182638",muted="#63748b",accent="#137b6b",ink="#ffffff",selected="#e1f1ed",red="#ba3744")}

def size(value):
    if value is None:return "Unknown size"
    value=float(value)
    for unit in ("B","KiB","MiB","GiB","TiB"):
        if value<1024 or unit=="TiB":return f"{value:.0f} {unit}" if unit=="B" else f"{value:.1f} {unit}"
        value/=1024

def asset(name):return Path(getattr(sys,"_MEIPASS",Path(__file__).resolve().parent.parent))/"assets"/name

def icon(name,color="#91a0b5",dimension=22):
    pm=QPixmap(dimension*2,dimension*2);pm.setDevicePixelRatio(2);pm.fill(Qt.GlobalColor.transparent)
    p=QPainter(pm);p.setRenderHint(QPainter.RenderHint.Antialiasing);p.scale(dimension/24,dimension/24)
    p.setPen(QPen(QColor(color),1.8,Qt.PenStyle.SolidLine,Qt.PenCapStyle.RoundCap,Qt.PenJoinStyle.RoundJoin))
    if name=="download":
        for a,b,c,d in [(12,3,12,15),(7,10,12,15),(17,10,12,15),(4,17,4,21),(4,21,20,21),(20,21,20,17)]:p.drawLine(a,b,c,d)
    elif name=="pause":p.drawLine(8,5,8,19);p.drawLine(16,5,16,19)
    elif name=="play":p.drawPolygon(QPolygonF([QPointF(7,4),QPointF(19,12),QPointF(7,20)]))
    elif name=="check":p.drawLine(4,12,10,18);p.drawLine(10,18,21,5)
    elif name=="torrent":p.drawArc(QRectF(5,4,14,15),180*16,180*16);p.drawLine(5,11,5,4);p.drawLine(19,11,19,4)
    elif name=="folder":p.drawPolyline(QPolygonF([QPointF(3,7),QPointF(3,4),QPointF(10,4),QPointF(13,7),QPointF(21,7),QPointF(21,20),QPointF(3,20),QPointF(3,7)]))
    elif name=="sun":
        import math
        p.drawEllipse(QRectF(8,8,8,8))
        for i in range(8):
            a=i*math.pi/4;p.drawLine(QPointF(12+7*math.cos(a),12+7*math.sin(a)),QPointF(12+10*math.cos(a),12+10*math.sin(a)))
    elif name=="moon":p.drawArc(QRectF(4,3,16,18),75*16,285*16);p.drawArc(QRectF(10,0,12,15),170*16,190*16)
    elif name=="settings":
        p.drawEllipse(QRectF(7,7,10,10));p.drawEllipse(QRectF(10,10,4,4))
        for a,b,c,d in [(12,2,12,6),(12,18,12,22),(2,12,6,12),(18,12,22,12),(5,5,7,7),(17,17,19,19),(5,19,7,17),(17,7,19,5)]:p.drawLine(a,b,c,d)
    elif name=="plus":p.drawLine(12,4,12,20);p.drawLine(4,12,20,12)
    elif name=="file":
        p.drawPolyline(QPolygonF([QPointF(6,2),QPointF(14,2),QPointF(20,8),QPointF(20,22),QPointF(6,22),QPointF(6,2)]));p.drawLine(14,2,14,8);p.drawLine(14,8,20,8);p.drawLine(9,13,16,13);p.drawLine(9,17,15,17)
    elif name=="dots":
        p.setBrush(QColor(color))
        for x in (5,12,19):p.drawEllipse(QRectF(x-1,11,2,2))
    else:p.drawEllipse(QRectF(4,4,16,16));p.drawLine(12,7,12,13);p.drawPoint(12,17)
    p.end();return QIcon(pm)

def app_icon():return QIcon(str(asset("enforcer.png"))) if asset("enforcer.png").is_file() else icon("download","#6ce4d0",64)
def label(text,style=""):
    w=QLabel(text);w.setObjectName(style);return w
def button(text,callback,primary=False):
    w=QPushButton(text);w.setMinimumHeight(38);w.setCursor(Qt.CursorShape.PointingHandCursor)
    if primary:w.setObjectName("primary")
    w.clicked.connect(callback);return w

class ElideLabel(QLabel):
    def __init__(self,text=""):
        super().__init__();self.full_text=text;self.setMinimumWidth(0);self.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Preferred);self.setText(text)
    def setText(self,text):
        self.full_text=text;self.setToolTip(text);super().setText(self.fontMetrics().elidedText(text,Qt.TextElideMode.ElideRight,max(30,self.width())))
    def resizeEvent(self,event):self.setText(self.full_text);super().resizeEvent(event)

class DownloadRow(QFrame):
    selected=Signal(str)
    def __init__(self,window,job):
        super().__init__();self.window=window;self.job_id=job.id;self.setObjectName("downloadRow");self.setMinimumHeight(118)
        layout=QHBoxLayout(self);layout.setContentsMargins(18,16,16,16);layout.setSpacing(16)
        self.tile=QLabel();self.tile.setObjectName("fileTile");self.tile.setFixedSize(44,44);self.tile.setAlignment(Qt.AlignmentFlag.AlignCenter);layout.addWidget(self.tile,0,Qt.AlignmentFlag.AlignTop)
        content=QVBoxLayout();content.setSpacing(7);head=QHBoxLayout()
        self.name=ElideLabel();self.name.setObjectName("rowTitle");self.state=label("","badge");head.addWidget(self.name,1);head.addWidget(self.state);content.addLayout(head)
        self.subtitle=ElideLabel();self.subtitle.setObjectName("muted");content.addWidget(self.subtitle)
        self.bar=QProgressBar();self.bar.setRange(0,1000);self.bar.setTextVisible(False);self.bar.setFixedHeight(5);content.addWidget(self.bar)
        self.metrics=ElideLabel();self.metrics.setObjectName("muted");content.addWidget(self.metrics);layout.addLayout(content,1)
        controls=QVBoxLayout();self.toggle=QToolButton();self.toggle.setFixedSize(34,34);self.toggle.clicked.connect(self.toggle_job)
        self.more=QToolButton();self.more.setFixedSize(34,34);self.more.clicked.connect(lambda:window.job_menu(self.job_id,self.more))
        for b in (self.toggle,self.more):b.setObjectName("rowAction");controls.addWidget(b)
        controls.addStretch();layout.addLayout(controls);self.update_job(job)
    def mousePressEvent(self,event):self.selected.emit(self.job_id);super().mousePressEvent(event)
    def toggle_job(self):
        j=self.window.jobs.get(self.job_id)
        if not j:return
        if j.state in ACTIVE|{"Queued"}:self.window.manager.stop(j.id)
        elif j.state=="Completed":self.window.reveal(j)
        else:self.window.manager.resume(j.id)
        self.window.refresh()
    def update_job(self,j):
        p=COLORS[self.window.theme];self.name.setText(j.display_name or Path(j.destination).name);self.state.setText(j.state)
        self.tile.setPixmap(icon("torrent" if j.kind=="torrent" else "file",p["accent"],28).pixmap(28,28))
        source="BITTORRENT" if j.kind=="torrent" else (urlsplit(j.url).hostname or "DIRECT FILE")
        self.subtitle.setText(source+(f"  ·  {j.file_count} files" if j.file_count else ""));self.bar.setValue(min(1000,int(1000*j.downloaded/j.total)) if j.total else 0)
        metrics=f"{size(j.downloaded)} / {size(j.total)}"
        if j.kind=='http':metrics+=f"   ·   {j.connections} connection{'s' if j.connections != 1 else ''}"
        if j.speed:metrics+=f"   ·   ↓ {size(j.speed)}/s"
        if j.kind=="torrent":metrics+=f"   ·   {j.peers} peers / {j.seeds} seeds   ·   ↑ {size(j.upload_speed)}/s"
        elif j.speed and j.total and j.state=="Downloading":
            seconds=max(0,int((j.total-j.downloaded)/j.speed));metrics+=f"   ·   {seconds//60}m {seconds%60}s left"
        self.metrics.setText(j.error or metrics);self.metrics.setStyleSheet(f"color:{p['red'] if j.error else p['muted']}")
        action="pause" if j.state in ACTIVE|{"Queued"} else "folder" if j.state=="Completed" else "play"
        self.toggle.setIcon(icon(action,p["text"],18));self.toggle.setToolTip({"pause":"Pause","folder":"Show in folder","play":"Resume / Retry"}[action]);self.more.setIcon(icon("dots",p["muted"],18))

class NewDownloadDialog(QDialog):
    def __init__(self,window,torrent=False):
        super().__init__(window);self.window=window;self.job=None;self.setWindowTitle("Add torrent" if torrent else "New download");self.setMinimumWidth(560)
        l=QVBoxLayout(self);l.setContentsMargins(28,24,28,24);l.setSpacing(12);l.addWidget(label("Add to your queue","dialogTitle"))
        self.mode=QComboBox();self.mode.addItems(["Direct file link","Magnet link",".torrent file"]);self.mode.setCurrentIndex(1 if torrent else 0);l.addWidget(self.mode)
        row=QHBoxLayout();self.source_edit=QLineEdit();row.addWidget(self.source_edit,1);self.browse_torrent=button("Choose file",self.choose_torrent);row.addWidget(self.browse_torrent);l.addLayout(row)
        l.addWidget(label("Save to","fieldLabel"));row=QHBoxLayout();self.folder_edit=QLineEdit(window.manager.settings()["folder"]);row.addWidget(self.folder_edit,1);row.addWidget(button("Browse",self.choose_folder));l.addLayout(row)
        self.name_label=label("Save as · optional","fieldLabel");l.addWidget(self.name_label);self.name_edit=QLineEdit();self.name_edit.setPlaceholderText("Keep the original name");l.addWidget(self.name_edit)
        self.hash_label=label("SHA-256 checksum · optional","fieldLabel");l.addWidget(self.hash_label);self.hash_edit=QLineEdit();self.hash_edit.setPlaceholderText("Verify against a trusted checksum");l.addWidget(self.hash_edit)
        self.seed_box=QCheckBox("Keep seeding after the download finishes");l.addWidget(self.seed_box)
        self.hint=label("","muted");self.hint.setWordWrap(True);l.addWidget(self.hint);self.error=label("","error");self.error.setWordWrap(True);l.addWidget(self.error)
        row=QHBoxLayout();row.addStretch();row.addWidget(button("Cancel",self.reject));self.start_button=button("Start download",self.submit,True);row.addWidget(self.start_button);l.addLayout(row)
        self.mode.currentIndexChanged.connect(self.update_mode);self.source_edit.returnPressed.connect(self.submit);self.source_edit.textChanged.connect(self.detect_magnet);self.update_mode()
    def detect_magnet(self,value):
        if value.strip().lower().startswith("magnet:") and self.mode.currentIndex()==0:self.mode.setCurrentIndex(1)
    def update_mode(self):
        m=self.mode.currentIndex();self.browse_torrent.setVisible(m==2);self.hash_edit.setVisible(m==0);self.hash_label.setVisible(m==0);self.seed_box.setVisible(m!=0)
        self.name_label.setText("Save as · optional" if m==0 else "Download folder label · optional")
        self.source_edit.setPlaceholderText(["https://example.com/file.zip","magnet:?xt=urn:btih:…","Choose a .torrent file"][m]);self.hint.setText("Direct file links only. YouTube video extraction is not included." if m==0 else "Torrents may upload pieces while downloading. Finished torrents stop unless you enable seeding.");self.adjustSize()
    def choose_torrent(self):
        path,_=QFileDialog.getOpenFileName(self,"Choose torrent",str(Path.home()/"Downloads"),"BitTorrent files (*.torrent)")
        if path:self.source_edit.setText(path)
    def choose_folder(self):
        path=QFileDialog.getExistingDirectory(self,"Choose download folder",self.folder_edit.text())
        if path:self.folder_edit.setText(path)
    def submit(self):
        try:
            if self.mode.currentIndex()==0:self.job=self.window.manager.add(self.source_edit.text(),self.folder_edit.text(),self.name_edit.text(),self.hash_edit.text())
            else:self.job=self.window.manager.add_torrent(self.source_edit.text().strip(),self.folder_edit.text(),name=self.name_edit.text(),seed=self.seed_box.isChecked())
        except (ValueError,OSError,RuntimeError) as exc:self.error.setText(str(exc));self.adjustSize();return
        self.window.filter="All downloads";self.window.search.clear();self.window.selected_id=self.job.id;self.window.refresh();self.accept()

class SettingsDialog(QDialog):
    def __init__(self,w):
        super().__init__(w);self.window=w;self.setWindowTitle("Settings");self.setMinimumWidth(540);settings=w.manager.settings()
        l=QVBoxLayout(self);l.setContentsMargins(28,24,28,24);l.setSpacing(10);l.addWidget(label("Your rules.","dialogTitle"));self.inputs={}
        for key,title,maximum in [("concurrency","Simultaneous downloads",4),("http_connections","Connections per HTTP file",8),("limit_kib","Download limit per transfer · KiB/s",1000000),("upload_limit_kib","Torrent upload limit per transfer · KiB/s",1000000)]:
            row=QHBoxLayout();row.addWidget(label(title),1);spin=QSpinBox();spin.setRange(1 if key in {"concurrency","http_connections"} else 0,maximum);spin.setValue(settings[key]);spin.setMinimumWidth(115);self.inputs[key]=spin;row.addWidget(spin);l.addLayout(row)
        l.addWidget(label("1 HTTP connection disables acceleration. Changes apply on start/resume.","muted"));l.addWidget(label("A speed limit of 0 means unlimited.","muted"));l.addWidget(label("Default download folder","fieldLabel"));self.folder=QLineEdit(settings["folder"]);l.addWidget(self.folder)
        def browse():
            path=QFileDialog.getExistingDirectory(self,"Default download folder",self.folder.text())
            if path:self.folder.setText(path)
        l.addWidget(button("Choose folder",browse));self.tray_box=QCheckBox("Keep downloads running in the tray when I close the window");self.tray_box.setChecked(w.close_to_tray);l.addWidget(self.tray_box)
        self.error=label("","error");self.error.setWordWrap(True);l.addWidget(self.error);row=QHBoxLayout();row.addStretch();row.addWidget(button("Cancel",self.reject));row.addWidget(button("Save settings",self.save,True));l.addLayout(row)
    def save(self):
        try:
            self.window.manager.configure(**{k:v.value() for k,v in self.inputs.items()},folder=self.folder.text());self.window.close_to_tray=self.tray_box.isChecked();self.window.manager.set_preference("close_to_tray",self.window.close_to_tray);self.window.refresh();self.accept()
        except (ValueError,OSError) as exc:self.error.setText(str(exc))

class EnforcerWindow(QMainWindow):
    def __init__(self,manager,*,enable_tray=True):
        super().__init__();self.manager=manager;self.jobs={};self.rows={};self.selected_id=None;self.filter="All downloads";self.closing=False;self.shutdown_thread=None
        self.theme=manager.preference("theme","dark");self.theme=self.theme if self.theme in COLORS else "dark";self.close_to_tray=bool(manager.preference("close_to_tray",True));self.tray_enabled=enable_tray
        self.setWindowTitle(f"{APP_NAME} {__version__}");self.setWindowIcon(app_icon());screen=QApplication.primaryScreen().availableGeometry();self.resize(min(1160,max(800,screen.width()-80)),min(760,max(500,screen.height()-80)));self.setMinimumSize(780,500)
        self._layout();self._tray();self.apply_theme();self.refresh();self.timer=QTimer(self);self.timer.timeout.connect(self.refresh);self.timer.start(350)
        self.new_shortcut=QShortcut(QKeySequence("Ctrl+N"),self);self.new_shortcut.activated.connect(self.add_dialog);self.find_shortcut=QShortcut(QKeySequence("Ctrl+F"),self);self.find_shortcut.activated.connect(self.search.setFocus)
    def _layout(self):
        outer=QWidget();self.setCentralWidget(outer);shell=QHBoxLayout(outer);shell.setContentsMargins(0,0,0,0);shell.setSpacing(0)
        side=QFrame();side.setObjectName("sidebar");side.setFixedWidth(194);sl=QVBoxLayout(side);sl.setContentsMargins(16,24,16,16);sl.setSpacing(6)
        brand=QHBoxLayout();brand.setSpacing(9);self.brand_icon=QLabel();self.brand_icon.setPixmap(app_icon().pixmap(38,38));brand.addWidget(self.brand_icon);bt=QVBoxLayout();bt.setSpacing(1);bt.addWidget(label("DOWNLOAD","brandSmall"));bt.addWidget(label("ENFORCER","brandName"));brand.addLayout(bt);sl.addLayout(brand);sl.addSpacing(29);sl.addWidget(label("WORKSPACE","sectionLabel"));sl.addSpacing(7)
        self.nav={}
        for title,graphic in [("All downloads","download"),("Active","play"),("Torrents","torrent"),("Completed","check"),("Paused","pause"),("Needs attention","alert")]:
            b=QPushButton(title);b.setObjectName("nav");b.setCheckable(True);b.setMinimumHeight(40);b.setProperty("graphic",graphic);b.clicked.connect(lambda checked=False,n=title:self.set_filter(n));self.nav[title]=b;sl.addWidget(b)
        sl.addStretch();self.theme_button=QPushButton();self.theme_button.setObjectName("nav");self.theme_button.setMinimumHeight(40);self.theme_button.clicked.connect(self.toggle_theme);sl.addWidget(self.theme_button)
        self.settings_button=QPushButton("Settings");self.settings_button.setObjectName("nav");self.settings_button.setMinimumHeight(40);self.settings_button.clicked.connect(self.settings_dialog);sl.addWidget(self.settings_button);sl.addSpacing(10);sl.addWidget(label(f"v{__version__}  ·  Under control.","version"));shell.addWidget(side)
        main=QWidget();main.setObjectName("main");ml=QVBoxLayout(main);ml.setContentsMargins(28,24,28,16);ml.setSpacing(18)
        h=QHBoxLayout();titles=QVBoxLayout();titles.setSpacing(3);titles.addWidget(label("Downloads","pageTitle"));self.subtitle=label("Your files. Under control.","muted");titles.addWidget(self.subtitle);h.addLayout(titles,1)
        self.torrent_button=button("Add torrent",lambda:self.add_dialog(torrent=True));self.new_button=button("New download",self.add_dialog,True);h.addWidget(self.torrent_button);h.addWidget(self.new_button);ml.addLayout(h)
        strip=QFrame();strip.setObjectName("activityStrip");al=QHBoxLayout(strip);al.setContentsMargins(16,11,16,11);self.down_stats=QLabel();self.up_stats=QLabel();self.active_stats=QLabel();al.addWidget(self.down_stats);al.addSpacing(26);al.addWidget(self.up_stats);al.addStretch();al.addWidget(self.active_stats);ml.addWidget(strip)
        h=QHBoxLayout();self.view_title=label("All downloads","viewTitle");h.addWidget(self.view_title,1);self.search=QLineEdit();self.search.setPlaceholderText("Search downloads…");self.search.setMaximumWidth(230);self.search.setMinimumWidth(140);self.search.textChanged.connect(self.refresh);h.addWidget(self.search)
        self.pause_all=QToolButton();self.pause_all.setFixedSize(36,36);self.pause_all.setToolTip("Pause all");self.pause_all.clicked.connect(lambda:self.manager.pause_all());h.addWidget(self.pause_all)
        self.resume_all=QToolButton();self.resume_all.setFixedSize(36,36);self.resume_all.setToolTip("Resume paused");self.resume_all.clicked.connect(lambda:self.manager.resume_all());h.addWidget(self.resume_all);ml.addLayout(h)
        self.stack=QStackedWidget();empty=QWidget();el=QVBoxLayout(empty);el.setContentsMargins(25,20,25,20);el.addStretch();self.empty_icon=QLabel();self.empty_icon.setPixmap(app_icon().pixmap(96,96));self.empty_icon.setAlignment(Qt.AlignmentFlag.AlignCenter);el.addWidget(self.empty_icon)
        self.empty_title=label("Nothing to chase. Yet.","emptyTitle");self.empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter);el.addWidget(self.empty_title);self.empty_hint=label("Drop a link into New download, or bring a torrent.","muted");self.empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter);self.empty_hint.setWordWrap(True);el.addWidget(self.empty_hint)
        h=QHBoxLayout();h.addStretch();h.addWidget(button("Add your first download",self.add_dialog,True));h.addStretch();el.addLayout(h);el.addStretch();self.stack.addWidget(empty)
        scroll=QScrollArea();scroll.setWidgetResizable(True);scroll.setFrameShape(QFrame.Shape.NoFrame);container=QWidget();container.setObjectName("listContainer");self.list_layout=QVBoxLayout(container);self.list_layout.setContentsMargins(0,0,6,0);self.list_layout.setSpacing(10);self.list_layout.addStretch();scroll.setWidget(container);self.stack.addWidget(scroll);ml.addWidget(self.stack,1)
        self.detail_panel=QFrame();self.detail_panel.setObjectName("detailsPanel");dl=QVBoxLayout(self.detail_panel);dl.setContentsMargins(14,10,14,10);dl.setSpacing(4);self.detail_title=ElideLabel();self.detail_title.setObjectName("fieldLabel");dl.addWidget(self.detail_title)
        self.destination_button=QPushButton();self.destination_button.setObjectName("pathButton");self.destination_button.setMinimumHeight(24);self.destination_button.clicked.connect(lambda:self.reveal(self.selected_job()));dl.addWidget(self.destination_button);self.detail_panel.hide();ml.addWidget(self.detail_panel)
        h=QHBoxLayout();self.footer=label("","version");h.addWidget(self.footer,1);self.tray_hint=label("Close to tray · downloads keep running","version");h.addWidget(self.tray_hint);ml.addLayout(h);shell.addWidget(main,1)
    def _tray(self):
        self.tray=QSystemTrayIcon(app_icon(),self);self.tray.setToolTip(APP_NAME);menu=QMenu();menu.addAction("Open Download Enforcer",self.show_from_tray);menu.addSeparator();menu.addAction("Pause all",self.manager.pause_all);menu.addAction("Resume paused",self.manager.resume_all);menu.addSeparator();menu.addAction("Quit",self.begin_shutdown);self.tray.setContextMenu(menu);self.tray_menu=menu
        self.tray.activated.connect(lambda r:self.show_from_tray() if r in {QSystemTrayIcon.ActivationReason.Trigger,QSystemTrayIcon.ActivationReason.DoubleClick} else None)
        if self.tray_enabled and QSystemTrayIcon.isSystemTrayAvailable():self.tray.show()
    def apply_theme(self):
        p=COLORS[self.theme];QApplication.instance().setStyleSheet(f"""
        QWidget {{color:{p['text']};font-family:'Segoe UI';font-size:13px;}}
        QMainWindow,QDialog,QWidget#main,QWidget#listContainer {{background:{p['bg']};}}
        QFrame#sidebar {{background:{p['side']};border-right:1px solid {p['line']};}}
        QLabel {{background:transparent;}} QLabel#brandSmall {{font-size:9px;letter-spacing:2px;color:{p['muted']};font-weight:600;}}
        QLabel#brandName {{font-size:16px;letter-spacing:0.7px;font-weight:800;}}
        QLabel#pageTitle {{font-size:26px;font-weight:700;}} QLabel#dialogTitle {{font-size:22px;font-weight:700;}}
        QLabel#viewTitle {{font-size:15px;font-weight:600;}} QLabel#muted,QLabel#version,QLabel#sectionLabel {{color:{p['muted']};}}
        QLabel#version {{font-size:10px;}} QLabel#sectionLabel {{font-size:9px;font-weight:700;letter-spacing:1.5px;padding-left:9px;}}
        QLabel#rowTitle {{font-size:14px;font-weight:600;}} QLabel#fieldLabel {{font-size:12px;font-weight:600;}}
        QLabel#emptyTitle {{font-size:21px;font-weight:600;padding:10px;}} QLabel#error {{color:{p['red']};}}
        QLabel#badge {{color:{p['accent']};background:{p['selected']};border-radius:5px;padding:3px 8px;font-size:11px;}}
        QLabel#fileTile {{background:{p['selected']};border-radius:11px;}}
        QFrame#activityStrip,QFrame#detailsPanel {{background:{p['panel']};border:1px solid {p['line']};border-radius:9px;}}
        QFrame#downloadRow {{background:{p['panel']};border:1px solid {p['line']};border-radius:10px;}}
        QFrame#downloadRow:hover {{border-color:{p['muted']};}}
        QPushButton,QToolButton {{background:{p['panel']};border:1px solid {p['line']};border-radius:7px;padding:8px 13px;font-weight:600;}}
        QPushButton:hover,QToolButton:hover {{background:{p['hover']};}} QPushButton:disabled {{color:{p['muted']};}}
        QPushButton#primary {{background:{p['accent']};color:{p['ink']};border:0;padding:9px 16px;}}
        QPushButton#nav {{background:transparent;border:0;text-align:left;padding:9px 10px;font-weight:500;}}
        QPushButton#nav:hover {{background:{p['hover']};}} QPushButton#nav:checked {{background:{p['selected']};color:{p['accent']};font-weight:600;}}
        QToolButton#rowAction {{padding:5px;border:0;}}
        QPushButton#pathButton {{border:0;background:transparent;color:{p['accent']};text-align:left;padding:0;font-size:11px;font-weight:400;}}
        QLineEdit,QSpinBox,QComboBox,QPlainTextEdit {{background:{p['panel']};border:1px solid {p['line']};border-radius:6px;padding:9px;selection-background-color:{p['selected']};}}
        QLineEdit:focus,QSpinBox:focus,QComboBox:focus {{border-color:{p['accent']};}} QComboBox QAbstractItemView {{background:{p['panel']};selection-background-color:{p['selected']};}}
        QCheckBox {{spacing:9px;padding:5px 0;}} QCheckBox::indicator {{width:17px;height:17px;border:1px solid {p['muted']};border-radius:4px;}}
        QCheckBox::indicator:checked {{background:{p['accent']};border:1px solid {p['accent']};}}
        QProgressBar {{background:{p['line']};border:0;border-radius:2px;}} QProgressBar::chunk {{background:{p['accent']};border-radius:2px;}}
        QScrollArea,QStackedWidget {{background:transparent;border:0;}} QScrollBar:vertical {{background:transparent;width:7px;margin:0;}}
        QScrollBar::handle:vertical {{background:{p['line']};border-radius:3px;min-height:25px;}} QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical {{height:0;}}
        QMenu {{background:{p['panel']};border:1px solid {p['line']};padding:5px;}} QMenu::item {{padding:8px 22px;border-radius:4px;}} QMenu::item:selected {{background:{p['selected']};}}
        QToolTip {{background:{p['panel']};color:{p['text']};border:1px solid {p['line']};padding:5px;}}
        """)
        for b in self.nav.values():b.setIcon(icon(b.property("graphic"),p['muted'],18));b.setIconSize(QSize(18,18))
        self.theme_button.setText("Light mode" if self.theme=="dark" else "Dark mode");self.theme_button.setIcon(icon("sun" if self.theme=="dark" else "moon",p['muted'],18));self.theme_button.setToolTip("Switch theme")
        self.settings_button.setIcon(icon("settings",p['muted'],18));self.pause_all.setIcon(icon("pause",p['muted'],18));self.resume_all.setIcon(icon("play",p['muted'],18));self.torrent_button.setIcon(icon("torrent",p['text'],18));self.new_button.setIcon(icon("plus",p['ink'],18))
        if os.name=='nt':
            try:
                dark=ctypes.c_int(1 if self.theme=='dark' else 0);ctypes.windll.dwmapi.DwmSetWindowAttribute(int(self.winId()),20,ctypes.byref(dark),ctypes.sizeof(dark))
            except (OSError,AttributeError):pass
        for job_id,row in self.rows.items():
            if job_id in self.jobs:row.update_job(self.jobs[job_id])
    def toggle_theme(self):self.theme='light' if self.theme=='dark' else 'dark';self.manager.set_preference('theme',self.theme);self.apply_theme()
    def set_filter(self,name):self.filter=name;self.refresh()
    def selected_job(self):return self.jobs.get(self.selected_id)
    def select(self,job_id):self.selected_id=job_id;self.update_detail()
    def refresh(self,*_):
        if self.closing:return
        jobs=self.manager.snapshot();self.jobs={j.id:j for j in jobs};active=sum(j.state in ACTIVE for j in jobs)
        self.down_stats.setText('↓  '+size(sum(j.speed for j in jobs))+'/s');self.up_stats.setText('↑  '+size(sum(j.upload_speed for j in jobs))+'/s');self.active_stats.setText(f"{active} active  ·  {sum(j.state=='Completed' for j in jobs)} completed");self.view_title.setText(self.filter)
        for name,b in self.nav.items():b.setChecked(name==self.filter)
        predicates={'All downloads':lambda j:True,'Active':lambda j:j.state in ACTIVE|{'Queued'},'Torrents':lambda j:j.kind=='torrent','Completed':lambda j:j.state=='Completed','Paused':lambda j:j.state=='Paused','Needs attention':lambda j:j.state in {'Error','Canceled'}}
        q=self.search.text().lower();visible=[j for j in jobs if predicates[self.filter](j) and q in (Path(j.destination).name+' '+j.url).lower()];ids={j.id for j in visible}
        for job_id in list(self.rows):
            if job_id not in ids:
                row=self.rows.pop(job_id);self.list_layout.removeWidget(row);row.deleteLater()
        for j in visible:
            if j.id not in self.rows:
                row=DownloadRow(self,j);row.selected.connect(self.select);self.rows[j.id]=row;self.list_layout.insertWidget(self.list_layout.count()-1,row)
            else:self.rows[j.id].update_job(j)
        self.stack.setCurrentIndex(1 if visible else 0);self.empty_title.setText('Nothing to chase. Yet.' if not jobs else 'All clear here.');self.empty_hint.setText('Drop a link into New download, or bring a torrent.' if not jobs else 'Try another view or a different search.')
        s=self.manager.settings();limit=f"{s['limit_kib']} KiB/s per transfer" if s['limit_kib'] else 'Unlimited download speed';self.footer.setText(f"{len(jobs)} items  ·  {s['concurrency']} at a time  ·  {limit}");self.tray_hint.setText('Close to tray · downloads keep running' if self.close_to_tray else 'Closing pauses transfers');self.tray.setToolTip(f'{APP_NAME}\n{active} active');self.update_detail()
    def update_detail(self):
        j=self.selected_job();self.detail_panel.setVisible(j is not None)
        if j:self.detail_title.setText(j.error or j.note or Path(j.destination).name);self.destination_button.setText(j.destination);self.destination_button.setToolTip('Show in folder: '+j.destination)
    def add_dialog(self,checked=False,*,torrent=False):
        d=NewDownloadDialog(self,torrent);d.open();return d
    def settings_dialog(self):
        d=SettingsDialog(self);d.open();return d
    def reveal(self,j):
        if not j:return
        path=Path(j.destination)
        try:
            if os.name=='nt':
                if path.exists():subprocess.Popen(['explorer.exe',f'/select,{path}'])
                else:os.startfile(str(path.parent))
            else:subprocess.Popen(['xdg-open',str(path if path.is_dir() else path.parent)])
        except OSError as exc:QMessageBox.warning(self,'Could not open folder',str(exc))
    def job_menu(self,job_id,anchor):
        self.select(job_id);j=self.jobs[job_id];m=QMenu(self);m.addAction('Show in folder',lambda:self.reveal(j));m.addAction('Details',lambda:self.details_dialog(j))
        if j.kind=='torrent':m.addAction('Stop seeding after completion' if j.seed_after_complete else 'Enable seeding',lambda:self.manager.set_seeding(j.id,not j.seed_after_complete))
        m.addSeparator()
        if j.state in ACTIVE|{'Queued'}:m.addAction('Pause',lambda:self.manager.stop(j.id))
        elif j.state!='Completed':m.addAction('Resume / Retry',lambda:self.manager.resume(j.id))
        if j.state!='Completed':m.addAction('Cancel download',lambda:self.manager.stop(j.id,cancel=True))
        remove=m.addAction('Remove from queue',lambda:self.remove(j));remove.setEnabled(j.state not in ACTIVE|{'Queued'});m.exec(anchor.mapToGlobal(anchor.rect().bottomLeft()))
    def remove(self,j):
        message='Torrent payloads will stay on disk.' if j.kind=='torrent' else 'The unfinished partial file will be removed. Completed files stay on disk.'
        if QMessageBox.question(self,'Remove from queue',message)==QMessageBox.StandardButton.Yes:
            try:self.manager.remove(j.id);self.refresh()
            except (ValueError,OSError) as exc:QMessageBox.warning(self,'Could not remove item',str(exc))
    def details_dialog(self,j):
        d=QDialog(self);d.setWindowTitle('Download details');d.resize(650,500);l=QVBoxLayout(d);l.setContentsMargins(24,24,24,24);l.addWidget(label('The details','dialogTitle'));info=QPlainTextEdit();info.setReadOnly(True)
        detail=f'Name\n{Path(j.destination).name}\n\nStatus\n{j.state}\n\nSource\n{j.url}\n\nSaved to\n{j.destination}\n\nReceived\n{size(j.downloaded)} / {size(j.total)}\n\n'
        if j.kind=='torrent':detail+=f"Torrent hashes\n{', '.join(j.torrent_hashes)}\n\n{j.file_count} files · {j.peers} peers · {j.seeds} seeds\nUploaded: {size(j.uploaded)}\nSeeding after completion: {'On' if j.seed_after_complete else 'Off'}\n\n"
        else:detail+=f"HTTP mode\n{j.connections} connections · {j.connection_mode}\n{j.acceleration_note}\n\nSHA-256\n{j.sha256 or 'Calculated when the download completes.'}\n\n"
        info.setPlainText(detail+(j.error or j.note));l.addWidget(info);l.addWidget(button('Close',d.accept,True));d.open();return d
    def show_from_tray(self):self.showNormal();self.raise_();self.activateWindow()
    def closeEvent(self,event):
        event.ignore()
        if self.closing:return
        if self.close_to_tray and self.tray_enabled and self.tray.isVisible():
            self.hide()
            if not self.manager.preference('tray_hint_seen',False):self.tray.showMessage(APP_NAME,'Still running. Downloads continue here. Use Quit in the tray menu to exit.',QSystemTrayIcon.MessageIcon.Information,3500);self.manager.set_preference('tray_hint_seen',True)
        else:self.begin_shutdown()
    def begin_shutdown(self):
        if self.closing:return
        self.closing=True;self.timer.stop();self.setEnabled(False);self.subtitle.setText('Saving transfers and stopping safely…');self.shutdown_thread=threading.Thread(target=self.manager.shutdown,daemon=False);self.shutdown_thread.start();self.shutdown_timer=QTimer(self);self.shutdown_timer.timeout.connect(self._finish_shutdown);self.shutdown_timer.start(100)
    def _finish_shutdown(self):
        if self.shutdown_thread and not self.shutdown_thread.is_alive():self.shutdown_timer.stop();self.tray.hide();self.hide();QApplication.instance().quit()
