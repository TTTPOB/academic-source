FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable --extra fast --extra vpnsci

FROM python:3.12-slim-bookworm
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    ACADEMIC_SOURCE_DATA_DIR=/data/academic-source
EXPOSE 8000
CMD ["academic-source", "serve", "--host", "0.0.0.0", "--port", "8000"]
