FROM python:3.12-slim AS builder
# 国内网络下 PyPI 直连会超时，默认使用阿里云镜像；构建时可用 --build-arg 覆盖。
ARG PYPI_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_PROJECT_ENVIRONMENT=/opt/venv \
    PIP_INDEX_URL=${PYPI_INDEX_URL} \
    UV_DEFAULT_INDEX=${PYPI_INDEX_URL}
WORKDIR /app
RUN pip install --no-cache-dir uv==0.11.16
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --locked --no-dev --extra postgres --extra llm-deepseek --no-editable

FROM builder AS test
COPY main.py ./main.py
COPY tests ./tests
RUN uv sync --locked --extra dev --extra postgres --extra llm-deepseek --no-editable
CMD ["/opt/venv/bin/pytest"]

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH=/opt/venv/bin:$PATH
WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
RUN addgroup --system metaagent && adduser --system --ingroup metaagent metaagent
USER metaagent
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health/live', timeout=3)"
CMD ["uvicorn", "meta_agent.app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
