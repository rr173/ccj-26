FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8001 8002 8003
# 三个服务共用同一镜像，由 docker-compose 的 command 决定角色
CMD ["uvicorn", "app.editor.main:app", "--host", "0.0.0.0", "--port", "8001"]
