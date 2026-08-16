# syntax=docker/dockerfile:1
# API Server 运行镜像：uv 管理依赖，uvicorn 直接跑 FastAPI 应用。

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    TIKTOKEN_CACHE_DIR=/tmp/tiktoken

WORKDIR /app

# 先只装第三方依赖（pyproject/uv.lock 不变时命中层缓存），再装项目本身。
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY nianlun ./nianlun
COPY app ./app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# data/ 挂载 SQLite、工作区与上传产物，运行时由 compose 卷接管。
RUN groupadd -r nianlun && useradd -r -g nianlun nianlun \
    && mkdir -p /app/data \
    && chown -R nianlun:nianlun /app
USER nianlun

EXPOSE 8000

# 直接调用 venv 内的 uvicorn，避免容器启动时触发 uv 重新解析依赖。
CMD [".venv/bin/uvicorn", "app.api_server.main:app", "--host", "0.0.0.0", "--port", "8000"]
