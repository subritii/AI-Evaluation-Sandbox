"""Air-gap check: prove the running stack can't reach the internet, and still works.

Run on the host with the stack up (docker compose up -d):
    .venv/bin/python scripts/airgap_check.py

What it checks, and why each part is needed:
  1. inventory     every running container's networks and published ports.
                   Only the proxy may be on a non-internal network or publish ports.
  2. in-container  egress probes (TCP to public IPs, DNS, TCP by hostname) run
                   inside each container that has Python (backend, dashboard).
  3. network       the same probe from a throwaway container on the sandbox
                   network, which covers db and ollama (no Python in those images).
  4. control       the same probe on Docker's default bridge. It must CONNECT:
                   otherwise "blocked" could just mean this machine is offline,
                   and the verdict is INCONCLUSIVE, not PASS.
  5. internal      the sandbox network still reaches db, ollama, backend, dashboard.
  6. ingress       the host reaches the dashboard and API through the proxy.
  7. proxy         whether the proxy itself has a route out (expected: yes; it is
                   the one dual-homed container). Reported, not hidden.
  8. functional    one real /query through the isolated stack succeeds.

Writes reports/airgap_check_<timestamp>.json. Exit code 0 = PASS, 1 = FAIL,
2 = INCONCLUSIVE.
"""

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
REPORTS_DIR = REPO_ROOT / "reports"
PROJECT = "ai-eval-sandbox"
SANDBOX_NETWORK = f"{PROJECT}_sandbox"
PROXY_SERVICE = "proxy"

# Egress attempts: raw IPs (no DNS needed), DNS itself, and a hostname the
# stack would plausibly call (the Ollama model registry). Prints one JSON object.
EGRESS_PROBE = r"""
import json, socket
results = {}
for label, host, port in (("tcp_1.1.1.1:443", "1.1.1.1", 443), ("tcp_8.8.8.8:53", "8.8.8.8", 53),
                          ("tcp_registry.ollama.ai:443", "registry.ollama.ai", 443)):
    try:
        socket.create_connection((host, port), timeout=5).close()
        results[label] = "CONNECTED"
    except Exception as exc:
        results[label] = "blocked: " + type(exc).__name__
try:
    results["dns_example.com"] = "RESOLVED " + socket.gethostbyname("example.com")
except Exception as exc:
    results["dns_example.com"] = "blocked: " + type(exc).__name__
print(json.dumps(results))
"""

# Reachability of the stack's own services from inside the sandbox network.
INTERNAL_PROBE = r"""
import json, socket, sys, urllib.request
results = {}
for label, host, port in (("db:5432", "db", 5432), ("ollama:11434", "ollama", 11434)):
    try:
        socket.create_connection((host, port), timeout=5).close()
        results[label] = "reachable"
    except Exception as exc:
        results[label] = "unreachable: " + type(exc).__name__
for label, url in (("backend /health", "http://backend:8000/health"),
                   ("dashboard /_stcore/health", "http://dashboard:8501/_stcore/health")):
    try:
        results[label] = "reachable (HTTP %d)" % urllib.request.urlopen(url, timeout=5).status
    except Exception as exc:
        results[label] = "unreachable: " + type(exc).__name__
print(json.dumps(results))
"""


def sh(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=REPO_ROOT, capture_output=True, text=True, timeout=timeout)


def run_probe(args: list[str], probe: str) -> dict:
    """Run a Python probe via `docker exec`/`docker run` args; parse its JSON output."""
    out = sh([*args, "python", "-c", probe], timeout=90)
    if out.returncode != 0:
        return {"error": (out.stderr or out.stdout).strip()[-300:]}
    return json.loads(out.stdout.strip().splitlines()[-1])


def any_egress(result: dict) -> bool:
    return any(str(v).startswith(("CONNECTED", "RESOLVED")) for v in result.values())


def all_egress(result: dict) -> bool:
    return bool(result) and "error" not in result and all(
        str(v).startswith(("CONNECTED", "RESOLVED")) for v in result.values()
    )


def inventory() -> list[dict]:
    """Running containers of this project with their networks (and whether each is internal) and ports."""
    out = sh(["docker", "compose", "ps", "--format", "json"])
    rows = [json.loads(line) for line in out.stdout.splitlines() if line.strip()]
    internal_cache: dict[str, bool] = {}
    containers = []
    for row in rows:
        info = json.loads(sh(["docker", "inspect", row["Name"]]).stdout)[0]
        networks = {}
        for name in info["NetworkSettings"]["Networks"]:
            if name not in internal_cache:
                net = json.loads(sh(["docker", "network", "inspect", name]).stdout)[0]
                internal_cache[name] = bool(net.get("Internal"))
            networks[name] = "internal" if internal_cache[name] else "external route"
        published = [
            f"{b['HostIp']}:{b['HostPort']}->{port}"
            for port, bindings in (info["NetworkSettings"].get("Ports") or {}).items()
            for b in (bindings or [])
        ]
        containers.append({
            "service": row["Service"], "container": row["Name"], "image": info["Config"]["Image"],
            "networks": networks, "published_ports": published,
            "isolated": all(v == "internal" for v in networks.values()),
        })
    return containers


def main() -> None:
    parser = argparse.ArgumentParser(description="Test that the running stack has no route to the internet.")
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--dashboard", default="http://localhost:8501")
    parser.add_argument("--skip-query", action="store_true", help="Skip the functional /query check")
    args = parser.parse_args()
    started = datetime.now(timezone.utc)
    # Recorded at the start: the code that runs is the code checked out now.
    git_commit = _git_commit()

    containers = inventory()
    if not containers:
        sys.exit("No running containers for this project. Start the stack: docker compose up -d")
    by_service = {c["service"]: c for c in containers}
    probe_image = (by_service.get("backend") or by_service.get("dashboard") or {}).get("image")
    if not probe_image:
        sys.exit("backend or dashboard must be running (their image carries the probe's Python).")

    checks: dict = {"inventory": containers}
    problems: list[str] = []

    # 1. Only the proxy may have an external route or published ports.
    for c in containers:
        if c["service"] == PROXY_SERVICE:
            continue
        if not c["isolated"]:
            problems.append(f"{c['service']} is on a network with an external route: {c['networks']}")
        if c["published_ports"]:
            problems.append(f"{c['service']} publishes ports: {c['published_ports']}")

    # 2. Egress from inside containers that have Python.
    checks["in_container"] = {}
    for service in ("backend", "dashboard"):
        if service in by_service:
            result = run_probe(["docker", "exec", by_service[service]["container"]], EGRESS_PROBE)
            checks["in_container"][service] = result
            if "error" in result:
                problems.append(f"probe in {service} failed to run: {result['error']}")
            elif any_egress(result):
                problems.append(f"{service} reached the internet: {result}")

    # 3. Egress from the sandbox network itself (covers db, ollama).
    checks["sandbox_network"] = run_probe(["docker", "run", "--rm", "--network", SANDBOX_NETWORK, probe_image], EGRESS_PROBE)
    if "error" in checks["sandbox_network"] or any_egress(checks["sandbox_network"]):
        problems.append(f"sandbox network probe: {checks['sandbox_network']}")

    # 4. Control: the same probe where egress is allowed must connect.
    checks["control_default_bridge"] = run_probe(["docker", "run", "--rm", "--network", "bridge", probe_image], EGRESS_PROBE)
    control_ok = all_egress(checks["control_default_bridge"])

    # 5. The stack still reaches its own services.
    checks["internal_reachability"] = run_probe(
        ["docker", "run", "--rm", "--network", SANDBOX_NETWORK, probe_image], INTERNAL_PROBE
    )
    for target, status in checks["internal_reachability"].items():
        if not str(status).startswith("reachable"):
            # ollama is legitimately absent in the native-Ollama configuration.
            if target.startswith("ollama") and "ollama" not in by_service:
                continue
            problems.append(f"internal: {target} {status}")

    # 6. Host ingress through the proxy.
    checks["ingress"] = {}
    gateway_info = None
    for label, url in (("dashboard", f"{args.dashboard}/_stcore/health"), ("api", f"{args.api}/health")):
        try:
            checks["ingress"][label] = f"HTTP {httpx.get(url, timeout=10).status_code}"
        except httpx.HTTPError as exc:
            checks["ingress"][label] = f"unreachable: {type(exc).__name__}"
            problems.append(f"ingress {label} unreachable")
    try:
        gateway_info = httpx.get(f"{args.api}/info", timeout=10).json()
    except (httpx.HTTPError, ValueError):
        pass

    # 7. The proxy: dual-homed by design. Report whether it can get out.
    if PROXY_SERVICE in by_service:
        out = sh(["docker", "exec", by_service[PROXY_SERVICE]["container"],
                  "wget", "-q", "-T", "5", "-O", "/dev/null", "http://1.1.1.1"], timeout=30)
        checks["proxy_egress"] = "CONNECTED" if out.returncode == 0 else "blocked"
    else:
        problems.append("proxy is not running")

    # 8. Functional: a real query through the isolated stack.
    if not args.skip_query:
        run_id = f"airgap-{started.strftime('%Y%m%d-%H%M%S')}"
        t = time.perf_counter()
        try:
            r = httpx.post(f"{args.api}/query", json={"question": "How long are KYC records kept?", "run_id": run_id},
                           timeout=600)
            body = r.json() if r.status_code == 200 else {}
            checks["functional_query"] = {"run_id": run_id, "status": r.status_code,
                                          "refused": body.get("refused"), "citations": len(body.get("citations", [])),
                                          "timings_ms": body.get("timings_ms"),
                                          "wall_s": round(time.perf_counter() - t, 1)}
            if r.status_code != 200:
                problems.append(f"functional query returned HTTP {r.status_code}")
        except httpx.HTTPError as exc:
            checks["functional_query"] = {"error": type(exc).__name__}
            problems.append("functional query failed")

    verdict = "FAIL" if problems else ("PASS" if control_ok else "INCONCLUSIVE")
    notes = []
    if not control_ok:
        notes.append("The control probe on Docker's default bridge could not reach the internet, so 'blocked' "
                     "results can't be told apart from this machine being offline.")
    if checks.get("proxy_egress") == "CONNECTED":
        notes.append("The ingress proxy can reach the internet (it must join a non-internal network to publish "
                     "ports). It runs nginx with a static, read-only config and no data; every container that "
                     "handles data has no route out.")

    report = {
        "kind": "airgap_check",
        "started_at": started.isoformat(),
        "git_commit": git_commit,
        "verdict": verdict,
        "problems": problems,
        "notes": notes,
        "model_backend": (gateway_info or {}).get("model_backend"),
        "gateway": gateway_info,
        "checks": checks,
    }
    REPORTS_DIR.mkdir(exist_ok=True)
    out_path = REPORTS_DIR / f"airgap_check_{started.strftime('%Y%m%d-%H%M%S')}.json"
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print("Containers:")
    for c in containers:
        nets = ", ".join(f"{n.removeprefix(PROJECT + '_')} ({kind})" for n, kind in c["networks"].items())
        print(f"  {c['service']:<10} {nets}{'  ports ' + ', '.join(c['published_ports']) if c['published_ports'] else ''}")
    for label, key in (("In-container egress", "in_container"),):
        for service, result in checks[key].items():
            print(f"{label} [{service}]: {result}")
    print(f"Sandbox network egress: {checks['sandbox_network']}")
    print(f"Control (default bridge): {checks['control_default_bridge']}")
    print(f"Internal reachability: {checks['internal_reachability']}")
    print(f"Ingress via proxy: {checks['ingress']}   proxy egress: {checks.get('proxy_egress')}")
    if "functional_query" in checks:
        print(f"Functional query: {checks['functional_query']}")
    print(f"Model backend (gateway): {report['model_backend']}")
    for p in problems:
        print(f"PROBLEM: {p}")
    for n in notes:
        print(f"Note: {n}")
    print(f"\nVerdict: {verdict}\nSaved {out_path.relative_to(REPO_ROOT)}")
    sys.exit({"PASS": 0, "FAIL": 1, "INCONCLUSIVE": 2}[verdict])


def _git_commit() -> str | None:
    try:
        commit = sh(["git", "rev-parse", "--short", "HEAD"]).stdout.strip()
        dirty = sh(["git", "status", "--porcelain"]).stdout
        return (commit + ("-dirty" if dirty.strip() else "")) or None
    except (OSError, subprocess.SubprocessError):
        return None


if __name__ == "__main__":
    main()
