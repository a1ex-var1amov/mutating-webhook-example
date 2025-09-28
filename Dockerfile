FROM python:3.11-slim

# Prevent Python from writing .pyc files (keeps layers smaller/cleaner)
# Stream stdout/stderr unbuffered for real-time logs in containers

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8443
ENV CERT_FILE=/tls/tls.crt \
    KEY_FILE=/tls/tls.key \
    PORT=8443

CMD ["python", "-m", "app.main"]
