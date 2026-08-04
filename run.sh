#!/usr/bin/env bash
# LocalizedTool 启动脚本（macOS / Linux）
cd "$(dirname "$0")"

if [ ! -x ".venv/bin/python" ]; then
    echo "尚未安装环境，请先运行 ./setup.sh"
    exit 1
fi

PORT="${PORT:-5000}"
echo "正在启动服务（Ctrl+C 停止）..."
.venv/bin/python app.py &
SERVER_PID=$!

echo "等待服务就绪..."
for i in $(seq 1 60); do
    if curl -s -o /dev/null "http://127.0.0.1:${PORT}"; then break; fi
    sleep 1
done

echo "打开浏览器..."
(open "http://127.0.0.1:${PORT}" || xdg-open "http://127.0.0.1:${PORT}" || true) >/dev/null 2>&1

wait "$SERVER_PID"
