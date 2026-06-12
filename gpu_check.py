#!/usr/bin/env python3
"""
gpu_check.py — Standalone GPU health check via nvidia-smi.

Queries all GPUs on a node (local or remote SSH), flags high temperature,
uncorrected ECC errors, and abnormal power states.

Usage:
    python gpu_check.py                          # local node
    python gpu_check.py --host gpu-node-01       # remote SSH
    python gpu_check.py --host gpu-node-01 --json
"""
from __future__ import annotations

import json
import argparse
import subprocess
import sys

TEMP_WARN = 85
TEMP_CRIT = 90


def query(host: str | None) -> list[dict]:
    cmd = [
        "nvidia-smi",
        "--query-gpu=index,name,temperature.gpu,memory.used,memory.total,"
        "utilization.gpu,ecc.errors.corrected.volatile.total,"
        "ecc.errors.uncorrected.volatile.total,power.draw,power.limit,pstate",
        "--format=csv,noheader,nounits",
    ]
    if host:
        cmd = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes",
               host] + cmd
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if out.returncode != 0:
        print(f"ERROR: nvidia-smi failed: {out.stderr.strip()[:200]}", file=sys.stderr)
        return []
    gpus = []
    for line in out.stdout.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 11:
            continue
        gpus.append({
            "index": parts[0], "name": parts[1],
            "temp_c": _int(parts[2]), "mem_used_mb": _int(parts[3]),
            "mem_total_mb": _int(parts[4]), "util_pct": _int(parts[5]),
            "ecc_corrected": _int(parts[6]), "ecc_uncorrected": _int(parts[7]),
            "power_w": _float(parts[8]), "power_limit_w": _float(parts[9]),
            "pstate": parts[10],
        })
    return gpus


def _int(v: str) -> int | None:
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


def _float(v: str) -> float | None:
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def check(gpus: list[dict]) -> list[dict]:
    issues = []
    for g in gpus:
        if g["ecc_uncorrected"] and g["ecc_uncorrected"] > 0:
            issues.append({"gpu": g["index"], "severity": "CRITICAL",
                           "msg": f"Uncorrected ECC errors: {g['ecc_uncorrected']}"})
        if g["temp_c"] and g["temp_c"] >= TEMP_CRIT:
            issues.append({"gpu": g["index"], "severity": "CRITICAL",
                           "msg": f"Temperature critical: {g['temp_c']}°C"})
        elif g["temp_c"] and g["temp_c"] >= TEMP_WARN:
            issues.append({"gpu": g["index"], "severity": "WARNING",
                           "msg": f"Temperature high: {g['temp_c']}°C"})
        if g["pstate"] not in ("P0", "P1", "P2", "N/A", None):
            issues.append({"gpu": g["index"], "severity": "WARNING",
                           "msg": f"Unexpected power state: {g['pstate']}"})
    return issues


def main():
    ap = argparse.ArgumentParser(description="GPU health check via nvidia-smi")
    ap.add_argument("--host", help="SSH to this host (default: local)")
    ap.add_argument("--json", action="store_true", dest="as_json")
    args = ap.parse_args()

    gpus = query(args.host)
    if not gpus:
        sys.exit(1)

    issues = check(gpus)

    if args.as_json:
        print(json.dumps({"gpus": gpus, "issues": issues}, indent=2))
        sys.exit(1 if any(i["severity"] == "CRITICAL" for i in issues) else 0)

    print(f"{'GPU':<5} {'Name':<30} {'Temp':>5} {'Util%':>5} "
          f"{'Mem MB':>8} {'ECC':>6} {'PState':<6}")
    print("-" * 70)
    for g in gpus:
        flag = " ⚠" if any(i["gpu"] == g["index"] for i in issues) else ""
        print(f"{g['index']:<5} {g['name']:<30} {str(g['temp_c'])+'°':>5} "
              f"{str(g['util_pct'])+'%':>5} "
              f"{str(g['mem_used_mb'])+'/'+str(g['mem_total_mb']):>8} "
              f"{g['ecc_uncorrected'] or 0:>6} {g['pstate']:<6}{flag}")

    if issues:
        print("\nIssues detected:")
        for i in issues:
            print(f"  [{i['severity']}] GPU {i['gpu']}: {i['msg']}")
        sys.exit(1)
    else:
        print("\nAll GPUs healthy.")


if __name__ == "__main__":
    main()
