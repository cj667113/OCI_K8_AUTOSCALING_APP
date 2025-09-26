# docker/Dockerfile
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libssl-dev ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Python deps for the app + Apache via mod_wsgi-express
RUN pip install --no-cache-dir flask prometheus_client kubernetes mod_wsgi

# Copy the Flask app folder
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
