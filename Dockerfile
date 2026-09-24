FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml uv.lock setup.py README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir uv && uv sync --frozen --no-dev --extra vpnsci --extra fast

ENV PATH="/app/.venv/bin:$PATH"
ENV ACADEMIC_SOURCE_DATA_DIR=/data/academic-source
EXPOSE 8000
CMD ["academic-source", "serve", "--host", "0.0.0.0", "--port", "8000"]
