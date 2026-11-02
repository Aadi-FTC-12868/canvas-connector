FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY packages ./packages
COPY pyproject.toml README.md LICENSE requirements.lock ./
COPY src ./src
RUN pip install --no-cache-dir -r requirements.lock ./packages/canvas-py ./packages/canvas-mcp-v2 . \
    && useradd --uid 10001 --create-home connector \
    && mkdir /data && chown connector:connector /data
USER connector
EXPOSE 8000
CMD ["canvas-connector", "serve", "--host", "0.0.0.0", "--port", "8000"]
