FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOST=0.0.0.0 PORT=10000 AFTERMARK_DB_PATH=/var/data/aftermark.db
WORKDIR /app
COPY server.py aftermark.html ./
EXPOSE 10000
CMD ["python", "server.py"]
