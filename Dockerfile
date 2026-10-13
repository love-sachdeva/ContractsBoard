FROM python:3.12-slim
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY app.py mcp_connections.py mcp_services.py ./
COPY static ./static
COPY scripts/container_start.py ./scripts/container_start.py
RUN useradd --create-home --uid 10001 portal && mkdir /data && chown portal:portal /data
ENV DATA_DIR=/data APP_ENV=production PORT=3000
EXPOSE 3000
# One worker owns rotating OAuth grants and SQLite. Persistent /data is mandatory.
# OAuth callback query strings must not be emitted to access logs.
CMD ["python", "scripts/container_start.py"]
