#!/bin/bash
# Amnezia DNS (Unbound) query-log rotation + age/size cleanup.
# Age: each run rotates non-empty active log into UTC-timestamped segment;
# delete segments whose START stamp is older than RETAIN_SEC (24h policy).
# Oldest retained record age ~= RETAIN_SEC + timer interval (+ jitter).
# Size: delete oldest archives until active+archives <= MAX_TOTAL_BYTES.
# Not a kernel hard quota; transient overshoot between runs is possible.
set -euo pipefail

CONTAINER="amnezia-dns"
LOG="/opt/unbound/etc/unbound/unbound.log"
ARCH_DIR="/opt/unbound/etc/unbound/log-archive"
RETAIN_SEC=$((24 * 3600))
MAX_TOTAL_BYTES=$((50 * 1024 * 1024))
LOCK="/run/amnezia-dns-log-cleanup.lock"

exec 9>"$LOCK"
if ! flock -n 9; then
  echo "another cleanup running" >&2
  exit 0
fi

if ! docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -qx true; then
  echo "container $CONTAINER not running" >&2
  exit 1
fi

TS=$(date -u +%Y%m%d%H%M%S)

docker exec "$CONTAINER" sh -c "mkdir -p '$ARCH_DIR' && chown _unbound:_unbound '$ARCH_DIR' && chmod 700 '$ARCH_DIR'"
docker exec "$CONTAINER" sh -c "touch '$LOG' && chown _unbound:_unbound '$LOG' && chmod 600 '$LOG'"

SIZE=$(docker exec "$CONTAINER" sh -c "stat -c %s '$LOG' 2>/dev/null || echo 0" | tr -d '\r')
SIZE=${SIZE:-0}

if [ "$SIZE" -gt 0 ]; then
  ARCH_NAME="unbound-${TS}.log"
  docker exec "$CONTAINER" sh -c "
    set -e
    cp '$LOG' '$ARCH_DIR/$ARCH_NAME'
    chown _unbound:_unbound '$ARCH_DIR/$ARCH_NAME'
    chmod 600 '$ARCH_DIR/$ARCH_NAME'
    : > '$LOG'
    chown _unbound:_unbound '$LOG'
    chmod 600 '$LOG'
  "
  echo "rotated ${SIZE} bytes -> $ARCH_NAME"
fi

export CONTAINER LOG ARCH_DIR RETAIN_SEC MAX_TOTAL_BYTES
python3 - <<'PY'
import os, re, subprocess
from datetime import datetime, timezone

container = os.environ["CONTAINER"]
arch = os.environ["ARCH_DIR"]
log = os.environ["LOG"]
retain = int(os.environ["RETAIN_SEC"])
max_total = int(os.environ["MAX_TOTAL_BYTES"])
now = datetime.now(timezone.utc)

def dexec(args):
    return subprocess.check_output(["docker", "exec", container] + args, text=True)

listing = dexec(["sh", "-c", f"ls -1 {arch}/unbound-*.log 2>/dev/null; stat -c %s {log} 2>/dev/null || echo 0"])
lines = [x for x in listing.splitlines() if x.strip()]
active_size = int(lines[-1]) if lines else 0
files = []
for p in lines[:-1]:
    base = os.path.basename(p)
    m = re.match(r"unbound-(\d{14})\.log$", base)
    if not m:
        print(f"skip weird name {base}")
        continue
    stamp = m.group(1)
    try:
        start = datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        print(f"cannot parse {base}")
        continue
    sz = int(dexec(["stat", "-c", "%s", p]).strip())
    age = (now - start).total_seconds()
    files.append((stamp, p, sz, age))

files.sort(key=lambda x: x[0])
removed_age = 0
kept = []
for stamp, p, sz, age in files:
    if age > retain:
        dexec(["rm", "-f", p])
        removed_age += 1
        print(f"deleted old archive {os.path.basename(p)} age={int(age)}s")
    else:
        kept.append((stamp, p, sz, age))

total = active_size + sum(sz for _, _, sz, _ in kept)
print(f"total_before_size_trim={total} active={active_size} archives={len(kept)} age_removed={removed_age}")
removed_size = 0
while total > max_total and kept:
    stamp, p, sz, age = kept.pop(0)
    dexec(["rm", "-f", p])
    total -= sz
    removed_size += 1
    print(f"size-deleted {os.path.basename(p)} ({sz} bytes)")
print(f"total_after={total} size_removed={removed_size}")
PY

docker exec "$CONTAINER" sh -c "
  echo -n active_bytes=; stat -c %s '$LOG' 2>/dev/null || echo 0
  echo -n archive_count=; ls -1 '$ARCH_DIR'/unbound-*.log 2>/dev/null | wc -l
  echo -n archive_bytes=; du -sb '$ARCH_DIR' 2>/dev/null | awk '{print \$1}'
"
