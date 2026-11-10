FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /opt/diplomacheck
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ ./app/
COPY config/ ./config/
COPY reference/ ./reference/
# Non-root account: uploaded student files are processed in temporary storage.
RUN useradd --create-home --uid 10001 botuser && chown -R botuser:botuser /opt/diplomacheck
USER botuser
CMD ["python", "-m", "app.bot_cloud"]
