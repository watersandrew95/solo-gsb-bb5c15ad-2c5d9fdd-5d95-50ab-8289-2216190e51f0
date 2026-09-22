FROM python:3.12-slim

WORKDIR /app

# 仅复制依赖清单与启动脚本；容器启动时自动安装依赖（见 entrypoint.sh）
COPY requirements.txt /app/requirements.txt
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh && mkdir -p /data

COPY app /app/app

EXPOSE 8000
ENTRYPOINT ["/app/entrypoint.sh"]
