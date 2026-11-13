FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    age ca-certificates curl git cmake make g++ libgomp1 libcurl4-openssl-dev \
    ffmpeg pkg-config sqlite3 chromium && rm -rf /var/lib/apt/lists/*
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
RUN pip install --no-cache-dir --editable '.[benchmarks]' pytest pytest-xdist playwright openai
COPY settings/defaults settings/defaults
COPY scripts scripts
COPY tests tests
ENV PLAYWRIGHT_CHROMIUM_EXECUTABLE=/usr/bin/chromium
ENTRYPOINT ["python", "/app/tests/docker/extraction/service.py"]
