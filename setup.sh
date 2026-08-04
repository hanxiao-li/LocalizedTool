#!/usr/bin/env bash
# LocalizedTool 一键环境安装（macOS / Linux）
set -e
cd "$(dirname "$0")"

echo "=============================================="
echo "  LocalizedTool - 环境安装"
echo "=============================================="

command -v python3 >/dev/null 2>&1 || { echo "[ERROR] 需要 Python 3.10+"; exit 1; }

if [ ! -d ".venv" ]; then
    echo "创建虚拟环境 .venv ..."
    python3 -m venv .venv
fi

echo "安装依赖（首次约需几分钟）..."
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install -r requirements.txt

echo "=============================================="
echo "  安装完成！运行 ./run.sh 启动服务。"
echo "  默认管理员: admin / admin123"
echo "=============================================="
