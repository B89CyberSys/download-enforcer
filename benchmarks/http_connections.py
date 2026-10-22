"""Reproducible loopback benchmark: python benchmarks/http_connections.py."""
from pathlib import Path
import hashlib
import json
import statistics
import sys
import tempfile
import time

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))
from fetchline.engine import DownloadManager
from test_segmented import RangeFixture, DATA


def main():
    results = []
    for scenario, rates in (("Per-connection cap: 1 MiB/s", {"rate": 1024**2}),
                            ("Shared server cap: 2 MiB/s", {"shared_rate": 2 * 1024**2})):
        for trial in range(3):
            for connections in ([1, 4] if trial % 2 == 0 else [4, 1]):
                with tempfile.TemporaryDirectory(prefix="enforcer-benchmark-") as directory:
                    fixture = RangeFixture(**rates)
                    manager = DownloadManager(Path(directory) / "queue.sqlite3", timeout=10)
                    try:
                        manager.configure(concurrency=1, limit_kib=0, folder=directory, http_connections=connections)
                        started = time.perf_counter()
                        job = manager.add(fixture.url, directory, "payload.bin")
                        while time.perf_counter() - started < 60:
                            live = manager.get(job.id)
                            if live.state in {"Completed", "Error"}:
                                break
                            time.sleep(.005)
                        elapsed = time.perf_counter() - started
                        assert live.state == "Completed", live.error
                        digest = hashlib.sha256(Path(live.destination).read_bytes()).hexdigest()
                        assert digest == hashlib.sha256(DATA).hexdigest() == live.sha256
                        row = dict(scenario=scenario, trial=trial + 1, connections=connections,
                                   seconds=round(elapsed, 4), bytes=len(DATA), sha256=digest,
                                   mode=live.connection_mode, max_active=fixture.max_active)
                        results.append(row)
                        print(json.dumps(row), flush=True)
                    finally:
                        manager.shutdown()
                        fixture.close()
    summaries = []
    for scenario in dict.fromkeys(r["scenario"] for r in results):
        medians = {n: statistics.median(r["seconds"] for r in results
                                     if r["scenario"] == scenario and r["connections"] == n) for n in (1, 4)}
        summaries.append(dict(scenario=scenario, single_seconds=medians[1], four_seconds=medians[4],
                              speedup=round(medians[1] / medians[4], 3)))
    report = dict(environment="Windows 11 x64, Python 3.13; controlled loopback HTTP server",
                  method="8 MiB generated payload, three trials per setting; alternate order; include probe, queue and SHA-256 time",
                  limitation="This measures controlled bottlenecks, not public internet speeds or a comparison against IDM/browser software.",
                  summaries=summaries, trials=results)
    target = PROJECT / "validation" / "http-benchmark.json"
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(report, indent=2), encoding="utf-8")
    lines = ["# HTTP connection benchmark", "", report["environment"], "", report["method"], "",
             "| Server condition | 1 connection (median) | 4 connections (median) | Speedup |",
             "|---|---:|---:|---:|"]
    for row in summaries:
        lines.append(f"| {row['scenario']} | {row['single_seconds']:.3f} s | {row['four_seconds']:.3f} s | {row['speedup']:.2f}x |")
    lines += ["", "Every output matched the generated source's SHA-256. Parallel trials used four overlapping server connections.",
              "", "Multiple connections help when a server limits each connection separately. They cannot exceed a shared server or internet connection limit.",
              "", report["limitation"], "", "Reproduce from the source folder: `python benchmarks/http_connections.py`."]
    target.with_suffix(".md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summaries, indent=2))


if __name__ == "__main__":
    main()
