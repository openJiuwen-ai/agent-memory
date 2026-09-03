#!/usr/bin/env sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo_root"

sh evaluation/setup.sh
compose_file=evaluation/environment/docker-compose.yml
docker compose -f "$compose_file" down --volumes --remove-orphans

redis_ready() {
  .venv/bin/python -c 'import socket; s=socket.create_connection(("127.0.0.1", 6379), 2); s.settimeout(2); s.sendall(b"*1\r\n$4\r\nPING\r\n"); raise SystemExit(0 if s.recv(64).startswith(b"+PONG") else 1)' >/dev/null 2>&1
}

http_ready() {
  .venv/bin/python -c 'import sys, urllib.request; response=urllib.request.urlopen(sys.argv[1], timeout=3); raise SystemExit(0 if response.status == 200 else 1)' "$1" >/dev/null 2>&1
}

set --
if redis_ready; then
  echo "Using existing Redis at 127.0.0.1:6379."
else
  set -- "$@" redis
fi
if http_ready 'http://127.0.0.1:9200/_cluster/health?wait_for_status=yellow&timeout=2s'; then
  echo "Using existing Elasticsearch at 127.0.0.1:9200."
else
  set -- "$@" elasticsearch
fi
if http_ready 'http://127.0.0.1:9091/healthz'; then
  echo "Using existing Milvus at 127.0.0.1:19530."
else
  set -- "$@" milvus
fi
if [ "$#" -gt 0 ]; then
  docker compose -f "$compose_file" up -d --wait "$@"
fi

export REDIS_URL=redis://127.0.0.1:6379/0
export ES_HOSTS=http://127.0.0.1:9200
export MILVUS_URI=http://127.0.0.1:19530
export MEM2_MILVUS_URI=http://127.0.0.1:19530
exec .venv/bin/python -m evaluation --config evaluation/config.yml
