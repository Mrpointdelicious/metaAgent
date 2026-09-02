FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir ".[postgres]"

FROM base AS test
COPY main.py ./main.py
COPY tests ./tests
RUN pip install --no-cache-dir ".[dev,postgres]"
CMD ["pytest"]

FROM base AS runtime
RUN addgroup --system metaagent && adduser --system --ingroup metaagent metaagent
USER metaagent
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health/live', timeout=3)"
CMD ["uvicorn", "meta_agent.app:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
