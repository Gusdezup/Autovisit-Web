FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install firefox && playwright install-deps firefox

COPY autovisit.py .
COPY app.py .
COPY templates/ templates/

ENV AUTOVISIT_DIR=/data
ENV AUTOVISIT_SCRIPT=/app/autovisit.py

EXPOSE 4567
CMD ["python3", "app.py"]
