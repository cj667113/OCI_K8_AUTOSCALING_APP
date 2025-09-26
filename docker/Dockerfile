# docker/Dockerfile
FROM ubuntu:latest

ENV DEBIAN_FRONTEND=noninteractive \
    PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    # Sensible defaults (override at runtime as needed)
    MAX_INFLIGHT=16 \
    ACQUIRE_TIMEOUT_MS=0 \
    CPU_MS=750 \
    PBKDF2_ITERS=200000

# System deps: Python, venv, Apache, mod_wsgi
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-venv python3-pip \
      apache2 libapache2-mod-wsgi-py3 \
      ca-certificates curl && \
    rm -rf /var/lib/apt/lists/*

# App virtualenv (avoids PEP 668 "externally-managed" issue)
RUN python3 -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir --upgrade pip setuptools wheel && \
    /opt/venv/bin/pip install --no-cache-dir \
      flask \
      prometheus_client \
      kubernetes

# App code (must include wsgi.py exporting "application")
WORKDIR /var/www/app
COPY flask/ /var/www/app/

# Apache vhost:
# - Listen on 8080
# - 16 mod_wsgi daemon processes, 1 thread each (maps well to 16 cores)
# - queue-timeout=0 => do NOT queue in mod_wsgi; if all workers busy, return 503
# - WSGIApplicationGroup %{GLOBAL} to avoid sub-interpreter overhead
# - No MPM limits here; let Apache accept connections freely and hand off to daemon
RUN printf 'ServerName localhost\n' >> /etc/apache2/apache2.conf && \
    printf 'Listen 8080\n' >> /etc/apache2/ports.conf && \
    a2enmod wsgi && \
    printf '%s\n' \
    "<VirtualHost *:8080>" \
    "    ServerName localhost" \
    "    # 16 single-threaded workers so the app can peg up to 16 CPU cores" \
    "    WSGIDaemonProcess app processes=16 threads=1 queue-timeout=0 " \
    "        python-home=/opt/venv python-path=/var/www/app display-name=%{GROUP}" \
    "    WSGIProcessGroup app" \
    "    WSGIScriptAlias / /var/www/app/wsgi.py" \
    "    WSGIApplicationGroup %{GLOBAL}" \
    "    <Directory /var/www/app>" \
    "        Require all granted" \
    "    </Directory>" \
    "    ErrorLog /dev/stderr" \
    "    CustomLog /dev/stdout combined" \
    "</VirtualHost>" \
    > /etc/apache2/sites-available/app.conf && \
    a2dissite 000-default.conf && \
    a2ensite app.conf

EXPOSE 8080
CMD ["apache2ctl", "-D", "FOREGROUND"]