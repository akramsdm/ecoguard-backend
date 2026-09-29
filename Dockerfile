FROM python:3.12-slim

# libgl1/libglib2.0-0: the OpenCV wheel that speciesnet depends on. libgomp1: PyTorch.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt requirements-ml.txt ./
# INSTALL_ML=false builds the much smaller image (~2 GB less) for a deployment that
# runs with IMAGE_ASSISTANCE=disabled. The classifier is imported lazily and the
# app reports "degraded" when its runtime is absent, so the smaller image is an
# explicit trade rather than a startup crash.
ARG INSTALL_ML=true
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt \
 && if [ "$INSTALL_ML" = "true" ]; then pip install -r requirements-ml.txt; fi

RUN useradd --create-home --uid 1000 app
COPY --chown=app:app app ./app
COPY --chown=app:app scripts ./scripts
# The entrypoint is invoked as `sh scripts/start.sh`, so the executable bit is not
# strictly required; it is set anyway so a host that execs the file also works.
RUN chmod +x scripts/start.sh
# Alembic migrations and config ship with the image: readiness compares the live
# revision against head, and containers may run `alembic upgrade head` themselves.
COPY --chown=app:app alembic.ini ./
COPY --chown=app:app alembic ./alembic
RUN mkdir -p /app/var/media /app/var/models /app/var/matplotlib && chown -R app:app /app/var

# kagglehub writes the Kaggle model download into this tree, so the named models volume
# caches the weights between restarts. They are fetched once, never per request.
ENV KAGGLEHUB_CACHE=/app/var/models \
    MPLCONFIGDIR=/app/var/matplotlib

USER app
# Documentation only: the platform injects PORT and routes to it. The entrypoint
# listens on ${PORT:-8000}.
EXPOSE 8000

# Reads the port from the environment like the app does, so the check stays correct
# when the platform assigns something other than 8000.
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
    CMD python -c "import os,urllib.request as u,sys;p=os.environ.get('PORT','8000');sys.exit(0 if u.urlopen(f'http://127.0.0.1:{p}/health',timeout=4).status==200 else 1)"

CMD ["sh","scripts/start.sh"]
