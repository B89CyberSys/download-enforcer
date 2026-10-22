# Download Enforcer 0.3.1 — validation

Checked on 22 October 2026 on Windows 11 x64 with Python 3.13.15, PySide6 6.11.1, libtorrent 2.1.1, and PyInstaller 6.21.0.

- **73 automated tests passed** in 46.325 seconds. The suite covers the HTTP engine, segmented transfers, queue persistence, the Qt interface, and local torrent transfers.
- A new upgrade regression test verifies that an older queue is backed up before migration, unexpected missing rows are recovered from the queue guard, and entries removed deliberately stay removed.
- The packaged 0.3.1 EXE passed its isolated native startup check, rendered both themes, opened all three dialogs, and reported zero startup network requests.
- The packaged EXE completed an 8,388,608-byte HTTP transfer with four overlapping local connections. Its SHA-256 matched `7d212b9c884f5c77896de960ae17cc341cda43b14d6a971f34ca29ebd4badf7f`.
- The same EXE completed a 2,097,184-byte generated local torrent transfer and matched both expected file hashes.
- No live user queue, browser data, public torrent, account, API key, or internet service was used by these checks.

The folder build requires its accompanying `_internal` directory. It is unsigned. `SHA256SUMS.txt` in the release ZIP identifies every shipped file.

## Earlier 0.3.0 validation

Checked on 21 October 2026 on Windows 11 x64 with Python 3.13.15, PySide6 6.11.1, libtorrent 2.1.1, and PyInstaller 6.21.0.

- **72 automated tests passed** in 51.949 seconds. See `validation/enforcer-tests.log`. This includes 36 existing HTTP/queue checks, 21 segmented HTTP checks, seven Qt interface checks, and eight torrent checks.
- Real loopback HTTP transfers verified four/eight disjoint ranges, single-connection settings, small-file fallback, missing/weak validators, strong Last-Modified dates, unsupported HEAD, ignored or inconsistent ranges, changing remote versions, transient errors, disconnected parts, checksums, safe removal, and a speed cap shared by all workers.
- Pause/restart and abrupt process exit recover only flushed per-part prefixes. Corrupt checkpoints and failure while replacing a checkpoint cannot cause a preallocated file's apparent length or old prefixes to be treated as verified content.
- The real Qt form completed a segmented HTTP transfer, displayed its connection count in Details, and saved the one-connection setting. Existing theme, tray, window size, and torrent form checks also passed.
- A torrent restart race found during the full suite was fixed: enabling seeding immediately after completion queues the new session even if the previous worker is still releasing its slot.
- **The packaged EXE completed an 8,388,608-byte HTTP download with four overlapping server connections**, reporting segmented mode. Its SHA-256 matched `7d212b9c884f5c77896de960ae17cc341cda43b14d6a971f34ca29ebd4badf7f`.
- The same EXE rendered both themes, opened all three dialogs, and completed the existing 2,097,184-byte local torrent transfer with matching payload hashes. See `validation/enforcer-packaged.json`. These optional transfer checks follow startup and use generated local payloads only.
- **Controlled benchmark:** one versus four connections took median **8.3174 s vs 2.3586 s (3.526x)** with a 1 MiB/s per-connection server cap. With a shared 2 MiB/s server cap, medians were **4.3018 s vs 4.2672 s (1.008x)**. Three trials per configuration used the same 8 MiB payload and included probing and final hashing. Every output matched the source SHA-256. Raw trials and reproducible source are supplied in `validation/http-benchmark.json` and `benchmarks/http_connections.py`.

Executable: `releases/0.3.0/DownloadEnforcer/DownloadEnforcer.exe`.
SHA-256: `206e6c317b5494b0959e1e3055f15e0ac4428491f28e7115c382fccd5e7306e8`.

The speed figures describe controlled local bottlenecks, not internet speed guarantees or an IDM/browser comparison. Actual gains depend on the origin and network. Acceleration currently requires suitable HEAD metadata, ranges, and strong validators; otherwise a single connection is used. The automated cases do not establish compatibility with every public server, proxy, filesystem, or Windows version. Public torrent swarms remain unbenchmarked. Browser capture and media extraction are not included.

The folder build requires its accompanying `_internal` directory. Shared libraries remain replaceable. `SHA256SUMS.txt` identifies the shipped files. The existing user queue is preserved at its original location.

## Earlier 0.2.0 validation

Checked on 21 October 2026 on Windows 11 x64 with Python 3.13.15, PySide6 6.11.1, libtorrent 2.1.1, and PyInstaller 6.21.0.

- **50 automated tests passed** in 27.469 seconds against the release source. `validation/enforcer-tests.log` records the result.
- Eight torrent integration tests used real local peers, a local tracker, and generated payloads. They cover .torrent files, magnet metadata exchange, piece verification, pause/restart/resume, seeding controls, duplicate hashes, invalid inputs, missing peers, and retaining payloads on removal.
- Six Qt interface tests cover adding real HTTP and torrent transfers through their forms, details and filters, a saved theme change, closing to the tray without stopping the manager, fitting essential controls at 800×540, and the cross-process instance lock.
- A completion race found during testing was fixed: the app waits for the torrent's disk-write barrier before presenting data as complete or seeding.
- The packaged Windows executable started successfully, rendered both themes, opened the download/torrent/settings dialogs, and completed a real local-peer torrent transfer. `validation/enforcer-packaged.json` records 2,097,184 bytes across two generated files and their SHA-256 hashes. The optional transfer check follows the startup stage; its network traffic was limited to local peers and the local tracker.
- The generated `payload.bin` SHA-256 was `91d3beb88a9b2f778a6c44a1c53b63d3c79931845a9aef84b3fb414610bd1938`, matching the seed's source bytes.
- Native widget renders were inspected in dark and light themes. See `validation/enforcer-dark.png` and `validation/enforcer-light.png`.
- A packaging conflict was diagnosed and fixed: an unrelated tool's ICU library had been selected from PATH. The build now uses a restricted process-local search path so Qt resolves Windows' compatible ICU library. No system PATH or runtime installation was changed.

Executable: `releases/0.2.0/DownloadEnforcer/DownloadEnforcer.exe`.
SHA-256: `dad47b7475f2ad0ed1de36a4c6d1ccf6b31845606ec903d2366ef4fa8bc1a9f0`.
This is a folder build with replaceable shared libraries; the executable requires its accompanying `_internal` folder. `SHA256SUMS.txt` in the release identifies all shipped files.

Public swarm performance, private trackers, proxies, and Windows versions beyond this test machine have not been validated. No public torrents were used for tests. Browser integration, media extraction, and selective torrent file downloading remain outside this release.

## Earlier 0.1.1 validation

Checked on 21 October 2026 on Windows 11, AMD64, using Python 3.13.15, Tk 8.6, and PyInstaller 6.21.0.

- **38 automated tests passed** in 11.252 seconds. See `validation/tests-0.1.1.log`.
- New regression coverage rejects YouTube page links before creating a job, rejects an HTML response when saving a video/file, allows intentional HTML downloads, and preserves direct-link extensions when custom names contain dotted version numbers.
- The native UI test also checks that the full destination path is visible for the selected download.
- The new packaged EXE passed isolated native startup and dialog checks, exit code 0. See `validation/startup-0.1.1.json`.
- Binary: `releases/0.1.1/Fetchline.exe`, 13,123,753 bytes. SHA-256: `dee4a7508b712255cb8c1b632959e09a8f94f4f558d33f444e6c96927fa41fac`.
- The previous app was kept running while the update was built; its active download was not paused or restarted.

This is a direct-file downloader. Video extraction and torrent support remain unimplemented. The HTML guard uses the response's declared Content-Type; it is not a universal file-content classifier. This update preserves extensions available in the direct URL; it does not infer every filename from redirects or server Content-Disposition headers.

## Earlier 0.1.0 validation

Checked on 21 October 2026 on Windows 11, AMD64, using Python 3.13.15, Tk 8.6, and PyInstaller 6.21.0.

## Results

- **32 automated tests passed** in 18.858 seconds. See `validation/tests.log`.
- A real native UI test added a download through the New download form, transferred the generated file, checked the completed row and checksum in Details, and exercised filters/search.
- Transfer tests covered HTTP 503 retries, permanent 404 handling, a broken connection, ordinary/unknown-length/chunked/empty responses, redirects, pause/resume, a server ignoring Range, invalid Content-Range, changed ETags, Last-Modified validators, and HTTP 416 fallback.
- Recovery was verified after a clean restart and after an abruptly terminated child process.
- Existing-file preservation was checked both before a transfer and when a destination file appeared during a transfer.
- Supplied checksum success/failure, queue concurrency, persisted settings, interruptible speed limiting, removal behavior, input sanitization, and cross-process instance locking were tested.
- **Real HTTPS transfer passed:** downloaded the public `https://www.python.org/static/favicon.ico`, 15,086 bytes; verified its ICO header. SHA-256: `39a32ed052ec937b46c40afcc3b87b98ecf2526cf8b6e2cd1698a8b4cd9a6e39`.
- **Packaged EXE startup passed**, exit code 0: the bundled runtime created the main native interface and New download / Settings dialogs and opened its isolated queue. See `validation/packaged-startup.json`.

The final icon/resource changes were checked through that packaged startup test; the 32-test suite preceded those cosmetic changes. No download engine changes followed the passing suite.

## Executable identity

- File: `Fetchline.exe`
- Size: 13,120,397 bytes
- SHA-256: `e58e30d547ef03685f3363f3bf11d5d626f4c9e328234b642aede42ea1b8bf5c`
- Unsigned, single-file Windows x64 executable.

## Limits of this evidence

The automated suite proves the listed controlled cases. It does not establish compatibility with every website, browser, Windows version, proxy, or disk/filesystem. Native GUI actions were automated against the application's widgets; human visual acceptance testing has not been completed. The real HTTPS transfer used the source engine; the packaged EXE check verified startup and bundled dependencies rather than a full internet transfer through the packaged UI.

Browser integration, segmented downloads, installers, signing, and automatic updates are not implemented in this release.
