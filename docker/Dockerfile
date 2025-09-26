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

# App code
WORKDIR /var/www/app
COPY flask/ /var/www/app/

# Provide a fallback wsgi.py if missing
# (expects your Flask app exposes `application` in app.py as you showed)
RUN if [ ! -f /var/www/app/wsgi.py ]; then \
      printf '%s\n' \
      'try:' \
      '    from app import application as application' \
      'except Exception:' \
      '    # Fallback: if your Flask object is named "app"' \
      '    from app import app as application' \
      > /var/www/app/wsgi.py ; \
    fi

# Apache vhost:
# - Listen on 8080
# - 16 mod_wsgi daemon processes, 1 thread each
# - queue-timeout=0 => do NOT queue; if all workers busy, return 503
# - WSGIApplicationGroup %{GLOBAL}
RUN printf 'ServerName localhost\n' >> /etc/apache2/apache2.conf && \
    printf 'Listen 8080\n' >> /etc/apache2/ports.conf && \
    a2enmod wsgi && \
    printf '%s\n' \
    "<VirtualHost *:8080>" \
    "    ServerName localhost" \
    "    # Single-threaded workers so PBKDF2 threads can peg cores" \
    "    WSGIDaemonProcess app processes=16 threads=1 queue-timeout=0 python-home=/opt/venv python-path=/var/www/app display-name=%{GROUP}" \
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