#!/usr/bin/env sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo_root"

if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
.venv/bin/python -m pip install -r evaluation/environment/requirements.txt

if [ ! -f evaluation/environment/.env ]; then
  cp evaluation/environment/.env.example evaluation/environment/.env
  echo "已创建 evaluation/environment/.env，请填写密钥后重新运行。"
  exit 2
fi

if [ "${1:-}" = "--run" ]; then
  exec sh evaluation/run.sh
fi
echo "环境准备完成。运行 ./evaluation/run.sh 开始端到端测试。"
