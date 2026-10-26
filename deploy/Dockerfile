FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN groupadd --system trader && useradd --system --gid trader --home /app trader
RUN mkdir -p /data && chown trader:trader /data
WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --upgrade pip && python -m pip install .

USER trader
EXPOSE 8000 8080
ENTRYPOINT ["prediction-trader"]
