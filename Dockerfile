FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CATGIRL_HOST=0.0.0.0 \
    CATGIRL_PORT=8000 \
    CATGIRL_DATA=/data

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"

# 云端版接在线 API，单 worker 足够（瓶颈在上游延迟，不在本机 CPU）；
# 想开多 worker 要先确认 SQLite 的写并发，记忆库是每用户一个 db 文件
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
