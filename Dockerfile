# mekoo-op — pure-Python payout & MCN task runner (ClickHouse → format → Telegram).
FROM python:3.12-slim

# ICT everywhere: payout stamps use local time and the scheduler fires on ICT
# clock times, so the container must agree with run/schedule.json.
ENV TZ=Asia/Ho_Chi_Minh \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first so the layer caches across source changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application source (.env, run/, .git are excluded via .dockerignore).
COPY . .

# run/ holds logs, schedule, dedup state, carryover surplus and generated PDFs.
# Declared here so it exists even before the compose volume is mounted.
RUN mkdir -p run

# Default entry = the control bot + scheduler ("run the project"). Override the
# command to run a one-off task or the config check.
CMD ["python3", "-m", "bot"]
