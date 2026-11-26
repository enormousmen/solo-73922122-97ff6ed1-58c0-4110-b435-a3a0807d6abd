FROM python:3.11-slim

# git is required to read the built-in merge history; the fixture repository
# itself is generated on first boot by app/fixture.py.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY wsgi.py ./wsgi.py

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HISTORY_REPO=/data/history-repo \
    WORK_ROOT=/tmp/gsb-bisect/work \
    TEST_DELAY_MIN=0.5 \
    TEST_DELAY_MAX=1.4 \
    PORT=5000

RUN mkdir -p /data /tmp/gsb-bisect/work

EXPOSE 5000 8000

# One worker: round/scheduler state is in-process memory. Multiple threads
# keep HTTP polling responsive while test worker threads execute git/QA.
# Bind both common ports inside the container for easy discovery.
CMD ["gunicorn", "--workers", "1", "--threads", "8", "--bind", "0.0.0.0:5000", "--bind", "0.0.0.0:8000", "--timeout", "0", "--access-logfile", "-", "--error-logfile", "-", "wsgi:application"]
