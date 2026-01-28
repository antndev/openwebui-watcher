FROM python:3.12-slim
WORKDIR /app
RUN mkdir -p /inbox
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY watcher.py /app/watcher.py
ENTRYPOINT ["python", "/app/watcher.py"]
