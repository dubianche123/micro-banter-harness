FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 REGULAR_HOME=/app DATA_DIR=/app/data
WORKDIR /app
COPY pyproject.toml requirements.txt README.md LICENSE ./
COPY *.py ./
COPY regular ./regular
RUN pip install --no-cache-dir . && useradd --uid 10001 --create-home regular && mkdir -p /app/data && chown regular:regular /app/data
USER 10001:10001
VOLUME ["/app/data"]
CMD ["regular", "run"]
