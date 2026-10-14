#!/usr/bin/env python3
"""Load benchmark (stdlib only). Gentle: <2min total, 1s pauses. No new deps."""

import concurrent.futures
import json
import os
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request

BASE = os.environ.get("LOAD_BASE_URL", "http://127.0.0.1:8080")
LEVELS = [1, 10, 50]
DURATION_S = float(os.environ.get("LOAD_DURATION_S", "5"))
PAUSE_S = 1.0
OUT_JSON = "/tmp/load-results.json"
TIMEOUT_S = 10


def req(method, path, token=None, payload=None):
    url = BASE + path
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(r, timeout=TIMEOUT_S) as resp:
            resp.read()
            status = resp.status
    except urllib.error.HTTPError as e:
        try:
            e.read()
        except Exception:
            pass
        status = e.code
    except Exception:
        status = 0
    dt = (time.perf_counter() - t0) * 1000.0
    return status, dt


def login(login, pin):
    status, _ = None, None
    url = BASE + "/api/v1/auth/login"
    data = json.dumps({"login": login, "pin": pin}).encode()
    r = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(r, timeout=TIMEOUT_S) as resp:
        body = json.loads(resp.read().decode())
        return body["access_token"]


def bench_one(method, path, token, payload, concurrency, duration_s, ok_statuses):
    deadline = time.perf_counter() + duration_s
    latencies: list[float] = []
    lock = threading.Lock()
    ok = 0
    err = 0
    status_counts: dict[int, int] = {}

    def worker():
        nonlocal ok, err
        local_lat: list[float] = []
        local_ok = 0
        local_err = 0
        local_sc: dict[int, int] = {}
        while time.perf_counter() < deadline:
            status, dt = req(method, path, token, payload)
            local_lat.append(dt)
            local_sc[status] = local_sc.get(status, 0) + 1
            if status in ok_statuses:
                local_ok += 1
            else:
                local_err += 1
        with lock:
            latencies.extend(local_lat)
            ok += local_ok
            err += local_err
            for k, v in local_sc.items():
                status_counts[k] = status_counts.get(k, 0) + v

    t_start = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as ex:
        futs = [ex.submit(worker) for _ in range(concurrency)]
        concurrent.futures.wait(futs)
    wall = time.perf_counter() - t_start
    n = len(latencies)
    lat_sorted = sorted(latencies)
    if n:
        p50 = lat_sorted[int(0.50 * (n - 1))]
        p99 = lat_sorted[int(0.99 * (n - 1))]
        avg = sum(latencies) / n
        mn = lat_sorted[0]
        mx = lat_sorted[-1]
    else:
        p50 = p99 = avg = mn = mx = 0.0
    rps = n / wall if wall > 0 else 0.0
    return {
        "n": n,
        "ok": ok,
        "err": err,
        "status_counts": {str(k): v for k, v in status_counts.items()},
        "wall_s": round(wall, 3),
        "rps": round(rps, 1),
        "avg_ms": round(avg, 1),
        "min_ms": round(mn, 1),
        "p50_ms": round(p50, 1),
        "p99_ms": round(p99, 1),
        "max_ms": round(mx, 1),
    }


def main():
    t_all = time.perf_counter()
    # health check
    for p in ("/health/live", "/health/ready"):
        try:
            status, _ = req("GET", p)
        except Exception as e:
            print(f"health check {p} failed: {e}", file=sys.stderr)
            sys.exit(1)
        print(f"{p} -> {status}")
    try:
        token = login("master1", "3333")
    except Exception as e:
        print(f"login master1 failed: {e}", file=sys.stderr)
        sys.exit(1)
    print("login master1: ok")

    endpoints = [
        {
            "name": "GET /dict/equipment",
            "method": "GET",
            "path": "/api/v1/dict/equipment",
            "token": token,
            "payload": None,
            "ok": {200},
        },
        {
            "name": "GET /analytics/dashboard",
            "method": "GET",
            "path": "/api/v1/analytics/dashboard",
            "token": token,
            "payload": None,
            "ok": {200},
        },
        {
            "name": "POST /auth/login (nouser)",
            "method": "POST",
            "path": "/api/v1/auth/login",
            "token": None,
            "payload": {"login": "nouser", "pin": "0000"},
            # invalid login returns 401 by design; count any answered HTTP as completed,
            # 401 is the expected "ok" outcome (seed is not touched).
            "ok": {401},
        },
        {
            "name": "GET /work-orders?limit=50",
            "method": "GET",
            "path": "/api/v1/work-orders?limit=50",
            "token": token,
            "payload": None,
            "ok": {200},
        },
    ]

    results: dict = {
        "base": BASE,
        "duration_s": DURATION_S,
        "levels": LEVELS,
        "endpoints": {},
    }
    for ep in endpoints:
        results["endpoints"][ep["name"]] = {}
        for c in LEVELS:
            print(f"bench {ep['name']} c={c} {DURATION_S}s ...", flush=True)
            r = bench_one(ep["method"], ep["path"], ep["token"], ep["payload"], c, DURATION_S, ep["ok"])
            results["endpoints"][ep["name"]][str(c)] = r
            print(f"  -> rps={r['rps']} p50={r['p50_ms']}ms p99={r['p99_ms']}ms n={r['n']} err={r['err']}")
            time.sleep(PAUSE_S)

    total_wall = time.perf_counter() - t_all
    results["total_wall_s"] = round(total_wall, 1)

    # stdout table
    print()
    hdr = f"{'endpoint':32s} {'conc':>4s} {'n':>5s} {'RPS':>7s} {'p50ms':>7s} {'p99ms':>7s} {'avg':>7s} {'err':>4s}"
    print(hdr)
    print("-" * len(hdr))
    for ep in endpoints:
        for c in LEVELS:
            r = results["endpoints"][ep["name"]][str(c)]
            print(
                f"{ep['name']:32s} {c:>4d} {r['n']:>5d} {r['rps']:>7.1f} "
                f"{r['p50_ms']:>7.1f} {r['p99_ms']:>7.1f} {r['avg_ms']:>7.1f} {r['err']:>4d}"
            )
    print(f"\ntotal wall: {total_wall:.1f}s -> {OUT_JSON}")

    with open(OUT_JSON, "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    _ = statistics  # keep import used (p50/p99 via order stats; module retained for ad-hoc use)


if __name__ == "__main__":
    main()
