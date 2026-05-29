FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Сначала зависимости — чтобы кэш слоёв работал эффективнее.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Затем исходники.
COPY . .

# Папка для базы данных SQLite (история диалогов).
RUN mkdir -p data

CMD ["python", "-m", "src.bot"]
