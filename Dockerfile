FROM nvidia/cuda:12.8.1-base-ubuntu22.04

COPY --from=ghcr.io/astral-sh/uv:0.11.6 /uv /usr/local/bin/uv
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV UV_PYTHON_INSTALL_DIR=/opt/python \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY backend/requirements.txt ./requirements.txt
RUN uv venv --python 3.11 --seed .venv \
    && .venv/bin/pip install --no-cache-dir -r requirements.txt

COPY backend/ ./backend/
CMD ["/bin/sh", "backend/start.sh"]
