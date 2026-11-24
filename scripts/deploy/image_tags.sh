#!/usr/bin/env bash
# Keep a rollback image for every deploy. Untagged (dangling) images are removed
# automatically on inforge-ops, so the previous image must carry a tag.
#
#   scripts/deploy/image_tags.sh pre        # before building: tag the RUNNING image as mnemo-server:<its commit>
#   scripts/deploy/image_tags.sh post       # after a successful deploy: tag the new image, keep the
#                                           # previous tag, remove older mnemo-server:* tags
#   scripts/deploy/image_tags.sh list
#   scripts/deploy/image_tags.sh rollback <commit>   # point compose's image at mnemo-server:<commit> and restart
#
# "Successful" means /v1/health is ok and reports the new commit; `post` checks this itself.
set -euo pipefail

COMPOSE_IMAGE="mnemo-server-mnemo-server:latest"
CONTAINER="mnemo-server-mnemo-server-1"
REPO="mnemo-server"
HEALTH="${MNEMO_HEALTH_URL:-http://127.0.0.1:8000/v1/health}"

commit_of() {  # BUILD_COMMIT baked into an image
    docker image inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$1" | sed -n 's/^BUILD_COMMIT=//p'
}

case "${1:-}" in
  pre)
    img="$(docker inspect -f '{{.Image}}' "$CONTAINER")"
    c="$(commit_of "$img")"
    [ -n "$c" ] && [ "$c" != unknown ] || { echo "running image has no BUILD_COMMIT; tag it by hand" >&2; exit 1; }
    docker tag "$img" "$REPO:$c"
    echo "running image tagged $REPO:$c"
    ;;
  post)
    c="$(commit_of "$COMPOSE_IMAGE")"
    h="$(curl -fsS -m 5 "$HEALTH")"
    echo "$h" | grep -q '"status":"ok"' || { echo "health not ok; not tagging" >&2; exit 1; }
    echo "$h" | grep -q "+$c\"" || { echo "health does not report commit $c; not tagging" >&2; exit 1; }
    running="$(docker inspect -f '{{.Image}}' "$CONTAINER")"
    [ "$running" = "$(docker image inspect -f '{{.Id}}' "$COMPOSE_IMAGE")" ] || { echo "container is not running $COMPOSE_IMAGE" >&2; exit 1; }
    docker tag "$COMPOSE_IMAGE" "$REPO:$c"
    # Keep the new tag and the most recent other tag (the previous deploy); drop older ones.
    mapfile -t tags < <(docker images "$REPO" --format '{{.CreatedAt}}\t{{.Tag}}' | sort -r | cut -f2 | grep -vx "$c" || true)
    keep_prev="${tags[0]:-}"
    for t in "${tags[@]:1}"; do docker rmi "$REPO:$t" >/dev/null && echo "removed old tag $REPO:$t"; done
    echo "deployed $REPO:$c; rollback image: ${keep_prev:+$REPO:}${keep_prev:-none}"
    ;;
  list)
    docker images "$REPO" --format '{{.Repository}}:{{.Tag}}\t{{.ID}}\t{{.Size}}\t{{.CreatedSince}}'
    ;;
  rollback)
    c="${2:?usage: rollback <commit>}"
    docker image inspect "$REPO:$c" >/dev/null
    docker tag "$REPO:$c" "$COMPOSE_IMAGE"
    docker compose up -d --no-build mnemo-server
    echo "rolled back to $REPO:$c; check $HEALTH (schema migrations are NOT rolled back)"
    ;;
  *)
    sed -n '2,12p' "$0"; exit 2 ;;
esac
