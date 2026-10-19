#!/usr/bin/env bash
set -euo pipefail
image=${1:?image reference required}
arch=${2:?HA architecture required}
expected=$arch
[[ "$arch" != aarch64 ]] || expected=arm64

docker pull "$image"
actual=$(docker image inspect "$image" --format '{{.Os}}/{{.Architecture}}')
[[ "$actual" == "linux/$expected" ]]
container=$(docker run --detach --network none --tmpfs /data --cap-drop ALL \
  --security-opt no-new-privileges "$image")
cleanup() {
  docker logs "$container"
  docker rm --force "$container" >/dev/null
}
trap cleanup EXIT

docker exec "$container" ipmitool -V
docker exec "$container" jq --version
docker exec -i "$container" python3 - <<'PY'
import time
from urllib.request import urlopen
for attempt in range(30):
    try:
        with urlopen("http://127.0.0.1:8099/health", timeout=2) as response:
            assert response.status == 200 and response.read() == b"ok"
        with urlopen("http://127.0.0.1:8099/", timeout=2) as response:
            assert response.status == 200 and b"iDRAC Controller" in response.read()
        print("Startup, health and dashboard OK")
        break
    except (OSError, AssertionError):
        if attempt == 29:
            raise
        time.sleep(1)
PY
