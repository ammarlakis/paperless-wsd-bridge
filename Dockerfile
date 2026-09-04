FROM python:3.14-slim@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WSD_PROFILES_DIR=/app/profiles \
    WSD_LISTEN_HOST=0.0.0.0 \
    WSD_LISTEN_PORT=6666

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY src/*.py /app/
COPY src/templates/ /app/templates/
RUN mkdir -p /app/profiles /spool \
    && chown -R 1000:1000 /app /spool

USER 1000:1000
EXPOSE 6666

CMD ["python", "/app/wsd-scan.py", "start"]
