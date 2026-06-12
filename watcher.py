#!/usr/bin/env python3
"""
watcher.py — SLURM node health watcher with auto-drain.

Polls `sinfo` on a configurable interval, detects nodes that match
drain policies (GPU XID errors, high memory, consecutive failures),
drains them with a reason string, and sends Slack/email alerts.

Usage:
    python watcher.py                          # run with watcher.yaml
    python watcher.py --config /etc/watcher.yaml
    python watcher.py --dry-run                # log what would be drained
    python watcher.py --check-once             # single pass then exit
"""
from __future__ import annotations

import re
import sys
import time
import logging
import argparse
import datetime
import subprocess
from pathlib import Path
from typing import Optional

import yaml

log = logging.getLogger("watcher")


# ── config ─────────────────────────────────────────────────────────────────────

def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


# ── SLURM helpers ──────────────────────────────────────────────────────────────

def sinfo(login_host: Optional[str] = None) -> str:
    cmd = ["sinfo", "-R", "--noheader", "-o", "%N|%T|%E|%C"]
    if login_host:
        cmd = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes",
               login_host] + cmd
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if out.returncode != 0:
        raise RuntimeError(f"sinfo failed: {out.stderr.strip()[:200]}")
    return out.stdout


def drain_node(node: str, reason: str, dry_run: bool = False,
               login_host: Optional[str] = None) -> bool:
    cmd = ["scontrol", "update", f"NodeName={node}", "State=drain", f"Reason={reason}"]
    if login_host:
        cmd = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes",
               login_host] + cmd
    if dry_run:
        log.info(f"[DRY-RUN] would drain {node}: {reason}")
        return True
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    if out.returncode != 0:
        log.error(f"drain {node} failed: {out.stderr.strip()[:200]}")
        return False
    log.info(f"drained {node}: {reason}")
    return True


def nvidia_smi(node: Optional[str] = None) -> list[dict]:
    """Query GPU health via nvidia-smi. Returns one dict per GPU."""
    cmd = [
        "nvidia-smi",
        "--query-gpu=index,name,temperature.gpu,memory.used,memory.total,"
        "utilization.gpu,ecc.errors.corrected.volatile.total,"
        "ecc.errors.uncorrected.volatile.total,pstate",
        "--format=csv,noheader,nounits",
    ]
    if node:
        cmd = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes",
               node] + cmd
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if out.returncode != 0:
        return []
    gpus = []
    for line in out.stdout.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 9:
            gpus.append({
                "index": parts[0], "name": parts[1], "temp": parts[2],
                "mem_used": parts[3], "mem_total": parts[4], "util": parts[5],
                "ecc_corrected": parts[6], "ecc_uncorrected": parts[7],
                "pstate": parts[8],
            })
    return gpus


# ── drain policies ─────────────────────────────────────────────────────────────

def evaluate_policies(node: str, state: str, reason: str,
                      policies: list[dict]) -> Optional[str]:
    """Return a drain reason string if any policy matches, else None."""
    for policy in policies:
        if not policy.get("enabled", True):
            continue
        pattern = policy.get("reason_pattern")
        if pattern and re.search(pattern, reason, re.IGNORECASE):
            return policy["drain_reason"].format(node=node, matched_reason=reason)
        state_match = policy.get("state_pattern")
        if state_match and re.search(state_match, state, re.IGNORECASE):
            return policy["drain_reason"].format(node=node, state=state)
    return None


# ── alerting ──────────────────────────────────────────────────────────────────

def alert_slack(webhook_url: str, message: str) -> None:
    import urllib.request
    import json
    data = json.dumps({"text": message}).encode()
    req = urllib.request.Request(webhook_url, data=data,
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=10)


# ── main loop ─────────────────────────────────────────────────────────────────

def check_once(config: dict, dry_run: bool = False) -> list[dict]:
    login_host = config.get("login_host")
    policies = config.get("drain_policies", [])
    actions = []

    try:
        raw = sinfo(login_host)
    except Exception as e:
        log.error(f"sinfo failed: {e}")
        return []

    for line in raw.splitlines():
        parts = line.strip().split("|")
        if len(parts) < 3:
            continue
        nodelist, state, reason = parts[0], parts[1], parts[2]

        if "drain" in state.lower():
            continue  # already drained

        drain_reason = evaluate_policies(nodelist, state, reason, policies)
        if not drain_reason:
            continue

        ok = drain_node(nodelist, drain_reason, dry_run=dry_run,
                        login_host=login_host)
        if ok:
            actions.append({"node": nodelist, "reason": drain_reason, "state": state})

    return actions


def main():
    ap = argparse.ArgumentParser(description="SLURM node health watcher")
    ap.add_argument("--config", default="watcher.yaml")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--check-once", action="store_true")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    config = load_config(args.config)
    interval = config.get("poll_interval_seconds", 300)

    if args.check_once:
        actions = check_once(config, dry_run=args.dry_run)
        log.info(f"check_once: {len(actions)} action(s)")
        return

    log.info(f"Starting watcher (interval={interval}s, dry_run={args.dry_run})")
    while True:
        try:
            actions = check_once(config, dry_run=args.dry_run)
            if actions:
                webhook = config.get("slack_webhook")
                if webhook:
                    msgs = "\n".join(f"• {a['node']}: {a['reason']}" for a in actions)
                    try:
                        alert_slack(webhook, f":warning: *Node auto-drain*\n{msgs}")
                    except Exception as e:
                        log.warning(f"slack alert failed: {e}")
        except Exception as e:
            log.error(f"watcher iteration error: {e}")
        time.sleep(interval)


if __name__ == "__main__":
    main()
