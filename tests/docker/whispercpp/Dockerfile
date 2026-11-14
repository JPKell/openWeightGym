FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends build-essential cmake git ffmpeg espeak-ng && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir '.[dev]'
CMD ["python", "tests/docker/whispercpp/qualify.py"]
