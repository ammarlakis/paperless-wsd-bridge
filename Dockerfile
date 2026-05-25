FROM python:3.12-slim@sha256:090ba77e2958f6af52a5341f788b50b032dd4ca28377d2893dcf1ecbdfdfe203

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
