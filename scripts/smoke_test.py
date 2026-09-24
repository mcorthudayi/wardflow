from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
API = os.getenv("WARDFLOW_API_URL", "http://localhost:5090")
results: list[bool] = []


def request(path: str, method: str = "GET") -> tuple[int, dict[str, str], object]:
    data = b"" if method == "POST" else None
    req = urllib.request.Request(API + path, method=method, data=data)
    with urllib.request.urlopen(req, timeout=15) as response:
        body = response.read()
        headers = {key.lower(): value for key, value in response.headers.items()}
        return response.status, headers, json.loads(body) if body else None


def check(name: str, test: Callable[[], object]) -> None:
    try:
        ok = bool(test())
        detail = ""
    except Exception as error:
        ok = False
        detail = f" ({error})"
    results.append(ok)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{detail}")


def api_psql(sql: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "exec", "wardflow-postgres", "psql", "-U", "wardflow_api", "-d", "wardflow",
         "-v", "ON_ERROR_STOP=1", "-Atc", sql],
        capture_output=True,
        text=True,
    )


def main() -> int:
    print(f"WardFlow smoke tests against {API}")
    try:
        overview = request("/api/overview")[2]
        forecast = request("/api/forecast")[2]
    except Exception as error:
        print(f"  FAIL  API is not reachable ({error})")
        return 1

    latest = overview.get("latest") or {}
    wards = [unit for unit in overview.get("units", []) if unit["kind"] == "ward"]

    print("API contract")
    check("GET /health returns 200", lambda: request("/health")[0] == 200)
    check("overview contains a KPI snapshot", lambda: bool(latest))
    check("overview lists 5 units", lambda: len(overview["units"]) == 5)
    check("occupancy is between 0 and 1", lambda: 0 < latest["occupancy"] <= 1)
    check("latest snapshot agrees with the live bed table (±6 beds)",
          lambda: abs(latest["bedsOccupied"] - sum(unit["occupied"] for unit in wards)) <= 6)
    check("KPI history returns snapshots", lambda: len(request("/api/kpis?hours=24")[2]) >= 12)
    check("recent events honours the limit", lambda: len(request("/api/events/recent?limit=5")[2]) == 5)
    check("boarding endpoint returns a list", lambda: isinstance(request("/api/boarding")[2], list))

    print("Forecast")
    check("forecast is ready", lambda: forecast["ready"] is True)
    check("forecast covers the next 6 hours", lambda: len(forecast["next"]) == 6)
    check("intervals contain the point estimate",
          lambda: all(point["low"] <= point["expected"] <= point["high"] for point in forecast["next"]))
    check("estimated daily ED arrivals are plausible (40-200)", lambda: 40 <= forecast["dailyRate"] <= 200)

    print("Real-time and security")
    check("SignalR hub negotiates a connection",
          lambda: "connectionToken" in request("/hubs/ops/negotiate?negotiateVersion=1", "POST")[2])
    check("responses send nosniff and DENY framing headers",
          lambda: (lambda h: h.get("x-content-type-options") == "nosniff" and h.get("x-frame-options") == "DENY")(request("/health")[1]))
    check("API database role can read", lambda: api_psql("SELECT count(*) FROM ops.visits").returncode == 0)
    check("API database role cannot modify data",
          lambda: "permission denied" in api_psql("DELETE FROM ops.visits WHERE false").stderr)
    check("API database role cannot create tables",
          lambda: "permission denied" in api_psql("CREATE TABLE ops.smoke_probe (id int)").stderr)

    print("Consistency")
    check("state invariants hold",
          lambda: subprocess.run([str(ROOT / "scripts" / "check_invariants.sh")], capture_output=True).returncode == 0)

    passed = sum(results)
    print(f"\n{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
