FROM mcr.microsoft.com/playwright/python:v1.55.0-noble
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py .
ENV PYTHONUNBUFFERED=1 DATA_DIR=/app/data
VOLUME ["/app/data"]
CMD ["python3","bot.py"]
