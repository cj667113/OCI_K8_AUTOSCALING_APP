# docker/Dockerfile
FROM ubuntu:latest

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    # Sensible defaults (override at runtime)
    MAX_INFLIGHT=16 \
    ACQUIRE_TIMEOUT_MS=0 \
    CPU_MS=750 \
    PBKDF2_ITERS=200000

# System deps: Python, Apache, mod_wsgi
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-pip \
      apache2 libapache2-mod-wsgi-py3 \
      ca-certificates curl && \
    rm -rf /var/lib/apt/lists/*

# App deps (system-wide; use break-system-packages to bypass PEP 668)
RUN pip3 install --no-cache-dir --break-system-packages \
      flask \
      prometheus_client \
      kubernetes

# App code (must include app.py; we’ll add a fallback wsgi.py)
WORKDIR /var/www/app
COPY flask/ /var/www/app/

# Provide a fallback wsgi.py that exposes "application"
RUN if [ ! -f /var/www/app/wsgi.py ]; then \
      printf '%s\n' \
      'try:' \
      '    from app import application as application' \
      'except Exception:' \
      '    from app import app as application' \
      > /var/www/app/wsgi.py ; \
    fi

# Apache vhost:
# - Listen on 8080
# - 16 mod_wsgi daemon processes, 1 thread each
# - queue-timeout=0 => do NOT queue; return 503 if all workers are busy
# - Use python-path to include the app dir
RUN printf 'ServerName localhost\n' >> /etc/apache2/apache2.conf && \
    printf 'Listen 8080\n' >> /etc/apache2/ports.conf && \
    a2enmod wsgi && \
    printf '%s\n' \
    "<VirtualHost *:8080>" \
    "    ServerName localhost" \
    "    WSGIDaemonProcess app processes=16 threads=1 queue-timeout=0 python-path=/var/www/app display-name=%{GROUP}" \
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