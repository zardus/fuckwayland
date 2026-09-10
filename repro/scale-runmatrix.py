#!/usr/bin/env python3
"""Host-side driver: apply a layout to a guest, then run probe.py in it.

  runmatrix.py <vm> <outdir> <config.json>

config.json = [{"label":..., "setup":[shell as user test, ...],
                "root_setup":[shell as root, ...],
                "heads": N | null, "fresh_daemon": true, "targets":[...]}]
"""
import json
import os
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VMCTL = os.path.join(REPO, "vm", "vmctl")

# ps|awk, never pkill: the pattern would otherwise match our own ssh command
# line and kill the session that is running it (vm/README.md, the __daemon note).
KILL_DAEMON = r"""ps -eo pid,args | awk '/__da[e]mon/ && !/awk/ {print $1}' | xargs -r kill; sleep 0.4"""


def run(args, timeout=180, check=False):
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        print(f"!! {' '.join(args[:6])}... rc={p.returncode}\n{p.stdout}\n{p.stderr}", file=sys.stderr)
    return p


def as_user(vm, cmd, timeout=120):
    return run([VMCTL, "user", vm, "--", "sh", "-lc", cmd], timeout=timeout)


def as_root(vm, cmd, timeout=120):
    return run([VMCTL, "ssh", vm, "--", cmd], timeout=timeout)


def main():
    vm, outdir, cfgfile = sys.argv[1], sys.argv[2], sys.argv[3]
    os.makedirs(outdir, exist_ok=True)
    configs = json.load(open(cfgfile))

    for cfg in configs:
        label = cfg["label"]
        print(f"=== {label}", flush=True)
        if cfg.get("heads") is not None:
            for idx, size in cfg["heads"]:
                run([VMCTL, "head", vm, str(idx), size], timeout=180)
            time.sleep(4)
        for c in cfg.get("root_setup", []):
            p = as_root(vm, c)
            print(f"  root: {c}\n    rc={p.returncode} {p.stdout.strip()[:300]} {p.stderr.strip()[:300]}", flush=True)
        for c in cfg.get("setup", []):
            p = as_user(vm, c)
            print(f"  user: {c}\n    rc={p.returncode} {p.stdout.strip()[:300]} {p.stderr.strip()[:300]}", flush=True)
        time.sleep(cfg.get("settle_after_setup", 3))
        if cfg.get("fresh_daemon", True):
            as_root(vm, KILL_DAEMON)

        probe_cfg = json.dumps({"label": label, "targets": cfg["targets"],
                                "settle": cfg.get("settle", 0.35)})
        p = as_root(vm, f"cd /root/w11 && W11=/root/w11 python3 probe.py {json.dumps(probe_cfg)}",
                    timeout=cfg.get("timeout", 300))
        path = os.path.join(outdir, label.replace("/", "_") + ".json")
        with open(path, "w") as f:
            f.write(p.stdout)
        ok = "ok" if p.returncode == 0 and p.stdout.strip().startswith("{") else "FAILED"
        print(f"  probe {ok} -> {path}", flush=True)
        if ok == "FAILED":
            print(p.stdout[:2000], file=sys.stderr)
            print(p.stderr[:2000], file=sys.stderr)


if __name__ == "__main__":
    main()
