# Download Enforcer 0.3.1

A Windows desktop download manager for direct HTTP/HTTPS links, magnet links, and local `.torrent` files. Previously named Fetchline. No account, AI model, paid service, or API key is used.

## Start

Use the **Download Enforcer** desktop shortcut. In a release ZIP, extract the entire folder and run `DownloadEnforcer.exe`. Keep the `_internal` directory next to the executable: it contains the bundled Python, Qt, and torrent libraries. Python does not need to be installed on the target computer. This is an unsigned Windows x64 prototype.

## Downloads and torrents

- **New download** or **Ctrl+N**: paste a direct file link. You can also paste a magnet link; the form detects it.
- **Add torrent**: select Magnet link or .torrent file, choose a destination, and start. **Choose file** opens a `.torrent` file from disk.
- Use the pause/resume button on a download row. Its three-dot menu provides file location, details, cancel, removal, and torrent seeding controls.
- Click a row to see its full destination below the list. Click the path to reveal it in Explorer.
- Torrents show peer/seed counts, download/upload rates, and file counts after metadata arrives. Availability depends on the torrent swarm; a magnet with no reachable peers can remain at Finding metadata.
- Each torrent gets its own new destination folder. All files in the torrent are downloaded; selective file downloading is not included yet.
- Completed torrents stop by default. Tick **Keep seeding after the download finishes** when adding one, or choose **Enable seeding** in its menu, to keep sharing. Pause stops sharing. Turning seeding off allows an already completed torrent to stop cleanly.
- Torrent pieces can upload while the torrent is downloading. **Settings** provides a per-torrent upload limit, initially 128 KiB/s. Zero means unlimited. No firewall rules, automatic router port mappings, or system startup entries are added by this app. Peer discovery and normal torrent connections are enabled only while a torrent is active.

## Theme and tray

The **sun/moon button at the lower left** switches between light and dark themes and remembers the choice.

Closing the window sends the app to the **system tray** by default; downloads continue. Click the tray icon to reopen it. Its menu includes Open, Pause all, Resume paused, and **Quit**. Quit saves and pauses work, then exits. Reopening leaves unfinished downloads paused until you choose Resume. Settings can disable closing to the tray. If a tray is unavailable, closing saves and exits instead of leaving an invisible app.

## HTTP file behavior

HTTP downloads support queues, pause/resume, automatic retries, restart recovery, speed limits, and optional trusted SHA-256 checksums. Resume requires suitable server support and validators; otherwise the download restarts safely. Existing destination files are preserved by choosing a new name.

**Acceleration is now included.** New direct HTTP/HTTPS downloads of at least **8 MiB** use **four connections** by default when the server supplies a reliable file version and correctly supports byte ranges. **Settings → Connections per HTTP file** allows 1–8 connections; choose 1 to disable acceleration. Changes apply on the next start/resume. Each HTTP row shows the connection count; Details explains the actual mode and any fallback.

The engine checks HEAD metadata and a one-byte range before starting parallel requests. A strong ETag, or sufficiently old Last-Modified date without an ETag, must identify the same representation throughout. Servers without suitable HEAD/range responses or validators use the ordinary single connection. If parallel requests become inconsistent, the engine discards those partial ranges and restarts safely as one stream. Small files also use one connection.

Parts write to separate positions in one preallocated partial file. Its apparent full size does **not** mean the download is complete. The adjacent `.segments.json` checkpoint records only flushed bytes per part. Pause and crash recovery revalidate the server's file version before reusing those bytes. Interrupted parts retry independently. All parts share the configured per-transfer speed limit; they do not each receive a separate allowance. SHA-256 is calculated before publication; supplying a trusted expected checksum also verifies the complete payload against it.

In the controlled local benchmark, an 8 MiB file took **8.317 s with one connection and 2.359 s with four** (medians of three trials; **3.53x**). With a shared server bandwidth cap, it took **4.302 s and 4.267 s**: essentially unchanged. Multiple connections can help a per-connection bottleneck; they cannot exceed your internet connection or a shared server limit. These are generated local test results, not a guaranteed internet speedup or an IDM/browser comparison. Reproduce the benchmark with `python benchmarks/http_connections.py`.

A custom name without a file extension keeps the extension from the direct URL, including names ending in version numbers such as `Ghidra 12.14`. Server-provided Content-Disposition filenames are not yet used.

YouTube video extraction is not included. YouTube watch/shorts/live/embed links receive an explanation. HTML responses are rejected when the requested destination is a non-HTML file, avoiding a webpage being presented as a successfully downloaded video or archive. Intentionally saved `.html`, `.htm`, and `.xhtml` files remain supported.

## Data and removal

To preserve the earlier queue, data remains at `%LOCALAPPDATA%\Fetchline\queue.sqlite3`. Theme and tray preferences are stored there too. URLs, including query tokens, are saved locally. Torrent metadata/checkpoints are in the adjacent `torrents` directory.

Before opening an older queue with a new app version, Download Enforcer creates a consistent SQLite backup in `%LOCALAPPDATA%\Fetchline\backups`. A second guarded copy of every active queue record is kept inside the database. If a queue row disappears without the app's explicit **Remove from queue** action, it is restored on the next start. Deliberate removal deletes both copies. Version 0.3.1 added this protection after an upgrade audit found that completed queue entries could not be proven to have survived an earlier update.

HTTP partial files are saved as `.fetchline-<id>.part` alongside their destination, with an adjacent `.segments.json` checkpoint for segmented transfers. Removing an HTTP entry deletes its unfinished partial file and checkpoint and keeps completed files. Removing a torrent entry **keeps its downloaded payload**; remove that folder yourself if you want to reclaim its disk space. Do not edit active partial files, checkpoints, or torrent payloads while they are transferring.

## Source, tests, and build

The supplied source includes the download engine, torrent adapter, Qt interface, and tests. Python 3.13 x64, PySide6 6.11.1, and libtorrent 2.1.1 are required for source execution. PyInstaller 6.21.0 was used to build this release.

```powershell
python -m pip install -r requirements.txt
python run.py
python -m unittest discover -s tests -v
python run.py --self-test validation\startup.json
.\build.ps1
```

Tests use generated files, a loopback HTTP server, and two real local torrent peers. They do not download public torrents. Restricted test environments need permission to connect to 127.0.0.1 and a writable temporary directory. `--data-dir <folder>` selects a separate queue for source or packaged app testing.

## Dependencies and licensing

Download Enforcer's own source is released under the MIT License; see `LICENSE`. The app bundles unmodified libtorrent and dynamically linked Qt/PySide6 libraries. Their separate license notices are in `licenses`, also copied alongside the release. The folder build leaves shared libraries replaceable. The application source and build instructions are supplied; they can be rebuilt against compatible replacement versions. See `THIRD-PARTY.md` for upstream source locations and notices. No commercial Qt license or paid service is required for this build's use of the open-source licensing option.

## Current limits

Browser capture/extensions, scheduling, authenticated browser cookies, media extraction, automatic updates, torrent file selection, and automatic Windows magnet/.torrent associations are not included. Torrent transfers were tested with generated local swarms; public swarm performance, private trackers, proxies, and wider Windows compatibility still need user acceptance testing. Segmentation deliberately falls back to one connection when HEAD metadata, byte ranges, or reliable validators are unavailable; it cannot force acceleration on every server.

## References

- [Libtorrent Python bindings](https://www.libtorrent.org/python_binding.html)
- [Libtorrent resume data](https://www.libtorrent.org/reference-Torrent_Handle.html#save_resume_data())
- [Qt for Python](https://doc.qt.io/qtforpython-6/)
- [HTTP semantics, RFC 9110](https://www.rfc-editor.org/rfc/rfc9110.html)
