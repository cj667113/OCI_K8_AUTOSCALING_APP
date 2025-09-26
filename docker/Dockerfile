# docker/Dockerfile
FROM ubuntu:latest

ENV DEBIAN_FRONTEND=noninteractive

# Install Python + Apache + mod_wsgi + system build deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-dev \
    apache2 libapache2-mod-wsgi-py3 \
    gcc g++ make curl wget \
    libssl-dev libffi-dev build-essential ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Python-only dependencies via pip
RUN pip3 install --no-cache-dir \
    flask \
    prometheus_client \
    kubernetes

# Copy your app into /var/www/app
WORKDIR /var/www/app
COPY flask/ /var/www/app/

# Apache site config for mod_wsgi
RUN printf '%s\n' \
    "<VirtualHost *:8080>" \
    "    ServerName localhost" \
    "    WSGIDaemonProcess app threads=16 python-path=/var/www/app" \
    "    WSGIScriptAlias / /var/www/app/wsgi.py" \
    "    <Directory /var/www/app>" \
    "        Require all granted" \
    "    </Directory>" \
    "    ErrorLog /dev/stderr" \
    "    CustomLog /dev/stdout combined" \
    "</VirtualHost>" \
    > /etc/apache2/sites-available/app.conf \
    && a2dissite 000-default.conf \
    && a2ensite app.conf

EXPOSE 8080

# Run apache in foreground
CMD ["apache2ctl", "-D", "FOREGROUND"]