FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CITYTWIN_DB_PATH=/app/data/citytwin.db

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
# The trained model binary is not committed (too large for git); build it into
# the image from the public CSV in data/ so the container serves forecasts.
RUN python train_model.py && mkdir -p /app/data

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
