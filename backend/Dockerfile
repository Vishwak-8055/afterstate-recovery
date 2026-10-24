# AFTERSTATE API and worker image. The worker needs the PostgreSQL *server* binaries because every restore
# runs in a disposable local cluster (see docs/adr/0001). Same image, different command.
FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gnupg postgresql-common util-linux \
 && sed -i 's/^#create_main_cluster.*/create_main_cluster = false/' /etc/postgresql-common/createcluster.conf \
 && /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y \
 && apt-get install -y --no-install-recommends postgresql-16 postgresql-client-16 \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.lock ./
RUN pip install -r requirements.lock
COPY pyproject.toml alembic.ini ./
COPY afterstate ./afterstate
COPY alembic ./alembic
RUN pip install --no-deps .
RUN useradd --system --uid 10001 --create-home --home-dir /home/afterstate afterstate \
 && mkdir -p /var/lib/afterstate/artifacts /var/lib/afterstate/scratch \
 && chown -R afterstate /var/lib/afterstate
USER afterstate
ENV AFTERSTATE_ARTIFACT_DIR=/var/lib/afterstate/artifacts AFTERSTATE_SCRATCH_DIR=/var/lib/afterstate/scratch AFTERSTATE_ENVIRONMENT=production
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --retries=5 CMD curl -fsS http://127.0.0.1:8000/api/v1/health/live || exit 1
CMD ["uvicorn", "afterstate.api.app:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
