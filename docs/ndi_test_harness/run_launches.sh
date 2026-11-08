#!/bin/bash
# N fresh launches, each one Begin Game (a game 1), and a stall count.
#
#   cd <folder holding the harness app_settings.toml>
#   R3_REPO=C:/git/r3-app/r3-app-poc DATA_DIR=C:/temp/r3-harness/data LAUNCHES=12 \
#       bash <this-file>
#
# DATA_DIR must be the base_data_dir from that app_settings.toml. It is wiped
# before every launch: a launch that ends without closing its session leaves it
# dirty, and the next one would stop on the resume dialog until a human clicks.
# The guard below refuses to wipe anything that is not an r3-harness folder.
#
# Windows open on screen and stay on top for ~35 s per launch. Don't run it
# alongside any other NDI pipeline on the same cameras (it taints both).
set -u
: "${R3_REPO:?set R3_REPO to the tree to test}"
: "${DATA_DIR:?set DATA_DIR to the harness base_data_dir}"
case "$DATA_DIR" in
  *r3-harness*) ;;
  *) echo "refusing to wipe '$DATA_DIR': not an r3-harness folder" >&2; exit 1 ;;
esac

export PATH="/c/msys64/ucrt64/bin:$PATH" PYTHONUTF8=1 PYTHONIOENCODING=utf-8
here="$(cd "$(dirname "$0")" && pwd)"
python=/c/msys64/ucrt64/bin/python.exe
launches="${LAUNCHES:-12}"
mkdir -p logs stacks
: > results.log

for i in $(seq 1 "$launches"); do
  rm -rf "$DATA_DIR"
  extra=$("$python" -c "import random; print(round(random.uniform(5, 20), 1))")
  timeout 170 "$python" "$here/launch_once.py" "$i" "$extra" > "logs/launch_$i.log" 2>&1
  grep -hE '^(RESULT|trial [0-9]+: STALL|WATCHDOG)' "logs/launch_$i.log" | tee -a results.log
  sleep 4
done

total=$(grep -c '^RESULT' results.log)
stalled=$(grep '^RESULT' results.log | grep -vc "stalled_at_7s=\[\]")
echo "launches with a result: $total; with a stalled feed at +7 s: $stalled"
