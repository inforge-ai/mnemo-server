#!/usr/bin/env bash
# Pre-build free-space check for deploys. Exits 1 (and prints why) unless the
# filesystem holding Docker's data root has room for a rebuild.
#
#   scripts/deploy/check_free_space.sh [image]      # default mnemo-server-mnemo-server:latest
#
# Required free space = max(MIN_FREE_GB, 2 x current image size + HEADROOM_GB).
# Why 2x: a rebuild writes the new layers next to the old image (~1x), plus
# uv's download/unpack space while installing dependencies (up to ~1x). The
# headroom keeps Postgres and Zulip (same disk on inforge-ops) writable even
# at the build's peak. On 2026-11-23 a build with 17 GB free and a 23.9 GB
# image filled the disk and took Zulip's RabbitMQ down for ~8 minutes.
#
# Overrides: MNEMO_BUILD_MIN_FREE_GB (default 30), MNEMO_BUILD_HEADROOM_GB (default 10).
# Also checks the temp directory's filesystem (compose writes metadata there).
set -euo pipefail

IMAGE="${1:-mnemo-server-mnemo-server:latest}"
MIN_FREE_GB="${MNEMO_BUILD_MIN_FREE_GB:-30}"
HEADROOM_GB="${MNEMO_BUILD_HEADROOM_GB:-10}"
GIB=1073741824

free_gb() {  # whole GiB available on the filesystem holding $1
    df -P -B1 "$1" | awk -v g="$GIB" 'NR == 2 { printf "%d", $4 / g }'
}

root="$(docker info -f '{{.DockerRootDir}}')"
img_bytes="$(docker image inspect -f '{{.Size}}' "$IMAGE" 2>/dev/null || echo 0)"
img_gb=$(( (img_bytes + GIB - 1) / GIB ))
need=$(( 2 * img_gb + HEADROOM_GB ))
(( need < MIN_FREE_GB )) && need=$MIN_FREE_GB
have="$(free_gb "$root")"
tmp="${TMPDIR:-/tmp}"
have_tmp="$(free_gb "$tmp")"

echo "docker root $root: ${have} GiB free; image $IMAGE: ${img_gb} GiB; required: ${need} GiB"
echo "temp dir $tmp: ${have_tmp} GiB free; required: 1 GiB"

fail=0
if (( have < need )); then
    echo "ABORT: ${have} GiB free on the Docker filesystem, need ${need} GiB. Free space first (see the runbook's prune list); do not build." >&2
    fail=1
fi
if (( have_tmp < 1 )); then
    echo "ABORT: less than 1 GiB free in $tmp." >&2
    fail=1
fi
(( fail == 0 )) && echo "OK: enough space to build."
exit "$fail"
