# SLURM Node Watcher

A lightweight Python daemon that continuously monitors SLURM cluster node health, auto-drains nodes matching configurable fault policies (XID GPU errors, hardware faults, FAIL state), and sends Slack alerts. Includes a standalone `gpu_check.py` for instant GPU health snapshots via `nvidia-smi`.

![Python](https://img.shields.io/badge/python-3.10+-blue) ![SLURM](https://img.shields.io/badge/scheduler-SLURM-orange)

---

## What it does

- **Continuous polling** — runs `sinfo -R` on a configurable interval
- **Policy-based auto-drain** — regex-match on node reason/state strings → `scontrol update State=drain`
- **GPU health check** — `nvidia-smi` query for temperature, ECC errors, power state
- **Slack alerts** — webhook notification on every auto-drain action
- **Dry-run mode** — logs what *would* be drained without taking action
- **Remote execution** — works via SSH to a login/slurmctld node; no agent needed

## Quick start

```bash
git clone https://github.com/pk-unix/slurm-node-watcher
cd slurm-node-watcher

# Configure
cp watcher.example.yaml watcher.yaml
$EDITOR watcher.yaml

# Test with a dry run
python watcher.py --dry-run --check-once

# Run as a daemon (or put in systemd/launchd)
python watcher.py
```

## GPU health check (standalone)

```bash
# Check local node
python gpu_check.py

# Check a remote node over SSH
python gpu_check.py --host gpu-node-01

# JSON output (exit code 1 if CRITICAL)
python gpu_check.py --host gpu-node-01 --json
```

Example output:
```
GPU   Name                           Temp  Util%   Mem MB    ECC PState
----------------------------------------------------------------------
0     NVIDIA H100 SXM5 80GB          72°    98%  75230/81920      0 P0
1     NVIDIA H100 SXM5 80GB          74°    97%  75180/81920      0 P0

All GPUs healthy.
```

## Drain policies

Policies are evaluated top-to-bottom in `watcher.yaml`. First match wins.

```yaml
drain_policies:
  - name: xid_error
    enabled: true
    reason_pattern: "XID|gpu_error"
    drain_reason: "auto-drain: GPU XID error [{matched_reason}]"

  - name: hw_fault
    enabled: true
    reason_pattern: "hardware_fault|mcelog"
    drain_reason: "auto-drain: hardware fault [{matched_reason}]"
```

## systemd unit

```ini
[Unit]
Description=SLURM Node Watcher
After=network.target

[Service]
ExecStart=/opt/slurm-watcher/venv/bin/python /opt/slurm-watcher/watcher.py
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
```

## Requirements

- Python 3.10+, PyYAML
- `sinfo` + `scontrol` accessible (local or via SSH)
- `nvidia-smi` for GPU checks
- SSH key auth to login/head nodes (for remote mode)
