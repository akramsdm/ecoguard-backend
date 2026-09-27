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
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt -r requirements-ml.txt

RUN useradd --create-home --uid 1000 app
COPY --chown=app:app app ./app
COPY --chown=app:app scripts ./scripts
RUN mkdir -p /app/var/media /app/var/models /app/var/matplotlib && chown -R app:app /app/var

# kagglehub writes the Kaggle model download into this tree, so the named models volume
# caches the weights between restarts. They are fetched once, never per request.
ENV KAGGLEHUB_CACHE=/app/var/models \
    MPLCONFIGDIR=/app/var/matplotlib

USER app
EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
    CMD python -c "import urllib.request as u,sys;sys.exit(0 if u.urlopen('http://127.0.0.1:8000/health',timeout=4).status==200 else 1)"

CMD ["uvicorn","app.main:app","--host","0.0.0.0","--port","8000"]
