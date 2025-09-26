# docker/Dockerfile
FROM ubuntu:latest

# Prevent tzdata / apt from prompting
ENV DEBIAN_FRONTEND=noninteractive

# Install build dependencies required by mod-wsgi-httpd (APR + httpd)
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-dev \
    gcc g++ make curl wget \
    libssl-dev libffi-dev libpcre3-dev build-essential ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps + mod-wsgi-httpd
RUN pip3 install --no-cache-dir \
    flask \
    prometheus_client \
    kubernetes \
    mod-wsgi-httpd

# Copy the Flask app folder (assumes build context is repo root)
WORKDIR /app
COPY flask/ /app/

# Single-process so Prometheus metrics don’t need multiprocess mode
ENV MOD_WSGI_PROCESSES=1 \
    MOD_WSGI_THREADS=16 \
    PORT=8080

# Generate a tiny Apache config and run under mod_wsgi-express
RUN mod_wsgi-express module-config > /tmp/mod_wsgi.conf && \
    printf '%s\n' \
    "ServerName localhost" \
    "Listen ${PORT}" \
    "WSGIDaemonProcess app processes=${MOD_WSGI_PROCESSES} threads=${MOD_WSGI_THREADS} python-path=/app" \
    "WSGIScriptAlias / /app/app.py" \
    "<Directory /app>" \
    "  Require all granted" \
    "</Directory>" \
    > /app/httpd.conf

EXPOSE 8080
CMD ["bash","-lc","mod_wsgi-express start-server --log-to-terminal --port ${PORT} --user www-data --group www-data /app/app.py -c /app/httpd.conf"]