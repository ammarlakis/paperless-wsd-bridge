FROM python:3.15-slim@sha256:94d3426f1def7db4a106a232ac121bfd975393e5dfd40a619b955fae45fe7913

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
