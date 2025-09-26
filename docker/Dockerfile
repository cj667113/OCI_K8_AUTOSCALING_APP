# docker/Dockerfile
FROM ubuntu:latest

ENV DEBIAN_FRONTEND=noninteractive

# System deps: Python, virtualenv, Apache, mod_wsgi
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip \
    apache2 libapache2-mod-wsgi-py3 \
    ca-certificates curl && \
    rm -rf /var/lib/apt/lists/*

# Create a virtualenv for app deps (avoids PEP 668 "externally-managed" error)
RUN python3 -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir --upgrade pip setuptools wheel && \
    /opt/venv/bin/pip install --no-cache-dir \
        flask \
        prometheus_client \
        kubernetes

# Copy your Flask app (must contain app.py and wsgi.py exporting "application")
WORKDIR /var/www/app
COPY flask/ /var/www/app/

# Apache site pointing to your WSGI app, using the venv
# Also ensure Apache listens on port 8080
RUN printf 'ServerName localhost\n' >> /etc/apache2/apache2.conf && \
    printf 'Listen 8080\n' >> /etc/apache2/ports.conf && \
    a2enmod wsgi && \
    printf '%s\n' \
    "<VirtualHost *:8080>" \
    "    ServerName localhost" \
    "    WSGIDaemonProcess app user=www-data group=www-data processes=64 threads=2 python-home=/opt/venv python-path=/var/www/app" \
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
