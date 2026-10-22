# Changelog

## 0.3.1 - 2026-10-22

- Back up an older queue database before an application upgrade.
- Keep a guarded copy of queue records and recover entries that disappear outside the app's deliberate removal flow.
- Verify queue IDs before and after startup normalization.
- Use the current application version in HTTP and torrent user-agent strings.
- Add regression coverage for backup, recovery, and deliberate removal behavior.

## 0.3.0 - 2026-10-21

- Add safe segmented HTTP transfers with up to eight connections.
- Add magnet and `.torrent` downloads through libtorrent.
- Add a native Qt interface, dark/light themes, tray behavior, transfer details, and connection settings.
- Add local HTTP, torrent, GUI, restart, integrity, and packaging checks.
