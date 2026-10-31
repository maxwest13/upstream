FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY upstream ./upstream
RUN pip install --no-cache-dir .
USER nobody
CMD ["python", "-m", "upstream.worker"]
