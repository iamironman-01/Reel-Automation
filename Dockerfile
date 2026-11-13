FROM python:3.11-slim

# Timezone matches config/schedules.yaml. PYTHONDONTWRITEBYTECODE keeps .pyc
# files out of the image and the bind mounts.
ENV TZ=Asia/Kolkata \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Run as a non-root user. The UID/GID must match the owner of the host's
# ./data and ./config folders, because they are bind-mounted into /app.
# Default is 1000, the first normal user on most Linux hosts. Override with:
#   docker compose build --build-arg APP_UID=$(id -u) --build-arg APP_GID=$(id -g)
# or set REELDROP_UID / REELDROP_GID in .env (used by docker-compose.yml).
# If the folders are owned by someone else, fix them on the host:
#   sudo chown -R 1000:1000 data config
ARG APP_UID=1000
ARG APP_GID=1000
RUN groupadd --gid "${APP_GID}" reeldrop \
 && useradd --uid "${APP_UID}" --gid "${APP_GID}" --create-home --home-dir /home/reeldrop --shell /usr/sbin/nologin reeldrop

# Streamlit writes to $HOME/.streamlit; keep it writable and quiet.
ENV HOME=/home/reeldrop \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=reeldrop:reeldrop . .
RUN mkdir -p /app/data /app/config && chown reeldrop:reeldrop /app /app/data /app/config

USER reeldrop

CMD ["python", "main.py"]
