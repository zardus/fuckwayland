#!/bin/bash
# Runs INSIDE the sway guest, for each of before/ and after/.
#   $1 = tree (before|after)
T=$1
cd /home/test/$T || exit 2
export PYTHONPATH=/home/test/$T W11_PASSTHROUGH=never
export XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-1
export SWAYSOCK=$(ls /run/user/1000/sway-ipc.*.sock | head -1)
W="python3 -m wxrandr"
geom() { swaymsg -t get_outputs | python3 -c '
import json,sys
print("    " + "  ".join("%s %dx%d+%d+%d s=%g" % (o["name"], o["rect"]["width"],
      o["rect"]["height"], o["rect"]["x"], o["rect"]["y"], o["scale"])
      for o in json.load(sys.stdin) if o["active"]))'; }
reset() { $W --backend sway --output Virtual-1 --auto --scale 1 --rotate normal --pos 0x0 \
    --output Virtual-2 --auto --scale 1 --rotate normal --right-of Virtual-1 \
    --output Virtual-3 --auto --scale 1 --rotate normal --right-of Virtual-2 >/dev/null 2>&1; }
echo "########## TREE=$T ##########"
reset

echo "--- hostile-2: a scale that truncates the logical size to 0 ---"
for bk in sway wlr; do
  for a in "--scale 99999" "--scale 200" "--scale-from 1x1"; do
    out=$(timeout -s KILL 40 $W --backend $bk --output Virtual-1 $a 2>&1); rc=$?
    printf '  [%-4s] %-20s rc=%-3s %s\n' "$bk" "$a" "$rc" "$(echo "$out"|head -1|cut -c1-70)"
    geom
    reset
  done
done

echo "--- layout-1: --dryrun --primary must not persist ---"
for bk in sway wlr; do
  rm -f $XDG_RUNTIME_DIR/wxrandr-state.json
  $W --backend $bk --output Virtual-1 --auto >/dev/null 2>&1
  b=$(cat $XDG_RUNTIME_DIR/wxrandr-state.json 2>/dev/null)
  $W --backend $bk --dryrun --output Virtual-2 --primary >/dev/null 2>&1
  a=$(cat $XDG_RUNTIME_DIR/wxrandr-state.json 2>/dev/null)
  [ "$b" = "$a" ] && v=UNCHANGED || v="CHANGED  -> $a"
  printf '  [%-4s] state after dryrun --primary: %s\n' "$bk" "$v"
  printf '  [%-4s] query says primary: %s\n' "$bk" \
      "$($W --backend $bk --query | grep -c ' primary ')"
  rm -f $XDG_RUNTIME_DIR/wxrandr-state.json
done
reset

echo "--- layout-2: --right-of must be edge to edge at fractional scales ---"
python3 - <<'PY'
import json, os, subprocess, sys
env = dict(os.environ)
def outs():
    return {o["name"]: o for o in json.loads(subprocess.run(
        ["swaymsg", "-r", "-t", "get_outputs"], capture_output=True,
        text=True, env=env).stdout)}
for bk in ("wlr", "sway"):
    bad = []
    for i in range(0, 201):
        s = 1.0 + i * 0.01
        subprocess.run([sys.executable, "-m", "wxrandr", "--backend", bk,
                        "--output", "Virtual-1", "--scale", "%.4f" % s,
                        "--pos", "0x0", "--output", "Virtual-2", "--scale",
                        "1", "--right-of", "Virtual-1"],
                       capture_output=True, text=True, env=env)
        o = outs()
        gap = o["Virtual-2"]["rect"]["x"] - (o["Virtual-1"]["rect"]["x"]
                                             + o["Virtual-1"]["rect"]["width"])
        if gap:
            bad.append((round(s, 2), gap))
    print("  [%-4s] wrong placements: %3d/201  %s" % (bk, len(bad), bad[:5]))
PY
reset

echo "--- layout-4: the plan must list outputs the normalisation moves ---"
$W --backend sway --dryrun --output Virtual-1 --pos -400x-400 2>&1 | sed 's/^/    /'

echo "--- hostile-3: --dpi 0 under --verbose/--dryrun ---"
for v in 0 nan -1; do
  out=$($W --backend sway --dryrun --dpi $v --output Virtual-1 --auto 2>&1); rc=$?
  printf '  --dpi %-4s rc=%-3s %s\n' "$v" "$rc" "$(echo "$out"|head -1|cut -c1-64)"
done

echo "--- hostile-4: non-finite arguments ---"
for a in "--output Virtual-1 --scale nan" "--output Virtual-1 --scale inf" \
         "--output Virtual-1 --rate 1e400" "-s nan"; do
  out=$($W --backend sway $a 2>&1); rc=$?
  printf '  %-34s rc=%-3s %s\n' "$a" "$rc" "$(echo "$out"|head -1|cut -c1-64)"
done
reset
echo "--- final layout ---"; geom
