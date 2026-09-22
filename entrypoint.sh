#!/bin/sh
# 容器启动时：1) 自动安装依赖  2) 自动建库  3) 启动服务
set -e

echo "[entrypoint] installing dependencies ..."
pip3 install --no-cache-dir -r /app/requirements.txt

echo "[entrypoint] initializing database ..."
EXAM_DB_PATH="${EXAM_DB_PATH:-/data/exam.db}" python3 -c "from app import database; database.init_db()"

echo "[entrypoint] starting API on 0.0.0.0:8000 ..."
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
