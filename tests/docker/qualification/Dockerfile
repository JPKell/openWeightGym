FROM postgres:16-bookworm AS pgtools
FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    age ca-certificates curl git cmake g++ libgomp1 libcurl4-openssl-dev \
    ffmpeg pkg-config libespeak-ng1 sqlite3 postgresql-client && rm -rf /var/lib/apt/lists/*
COPY --from=pgtools /usr/lib/postgresql/16/bin/pg_dump /usr/lib/postgresql/16/bin/pg_dump
COPY --from=pgtools /usr/lib/postgresql/16/bin/pg_restore /usr/lib/postgresql/16/bin/pg_restore
WORKDIR /app
COPY pyproject.toml .
COPY docs docs
COPY engines engines
COPY mirrorwall mirrorwall
COPY soundroom soundroom
COPY db db
COPY loadcoach loadcoach
COPY benchmarks benchmarks
COPY toollocker toollocker
COPY weightroom weightroom
COPY spotter spotter
COPY turnstile turnstile
COPY voice voice
COPY plugins plugins
COPY settings/defaults settings/defaults
RUN pip install --no-cache-dir '.[benchmarks,postgresql,voice,voice-gateway]'
COPY scripts scripts
COPY tests/docker/qualification/service.py /app/qualification.py
ENTRYPOINT ["python", "/app/qualification.py"]
