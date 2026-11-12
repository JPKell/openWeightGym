FROM python:3.13-slim
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
RUN pip install --no-cache-dir .
COPY tests/docker/images/service.py /app/service.py
ENTRYPOINT ["python", "/app/service.py"]
