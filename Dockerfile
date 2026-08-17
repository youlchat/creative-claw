FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CREATIVE_CLAW_DB=/data/creative-claw.db \
    CREATIVE_CLAW_PROJECT_ROOT=/data/projects/demo \
    PORT=8766

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY creative_claw ./creative_claw
RUN pip install --no-cache-dir .

COPY examples/bootstrap_demo.py ./examples/bootstrap_demo.py
COPY story-sources ./story-sources
COPY scripts/docker-entrypoint.sh /usr/local/bin/creative-claw-entrypoint
RUN chmod +x /usr/local/bin/creative-claw-entrypoint \
    && mkdir -p /data \
    && useradd --create-home --uid 10001 creativeclaw \
    && chown -R creativeclaw:creativeclaw /data

USER creativeclaw
EXPOSE 8766
VOLUME ["/data"]

HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=5 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.getenv('PORT','8766')+'/health', timeout=2)" || exit 1

ENTRYPOINT ["creative-claw-entrypoint"]
