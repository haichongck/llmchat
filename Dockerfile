# llmchat —— 零依赖容器镜像（仅 Python 标准库）
FROM python:3.12-slim

WORKDIR /app
COPY index.html server.py ./
COPY vendor ./vendor

ENV PYTHONUNBUFFERED=1 \
    LLMCHAT_HOST=0.0.0.0 \
    LLMCHAT_PORT=8080 \
    OLLAMA_BASE_URL=http://127.0.0.1:11434 \
    LLMCHAT_SERVICE_CONTROL=0

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
  CMD python3 -c "import sys,urllib.request;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/',timeout=2).status==200 else 1)"

CMD ["python3", "server.py"]
