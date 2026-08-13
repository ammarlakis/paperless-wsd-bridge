FROM python:3.14-slim@sha256:ce40764625a4ff50df3548277632e7f96c4e77fe75fa848aae9885476e7df5a4

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
