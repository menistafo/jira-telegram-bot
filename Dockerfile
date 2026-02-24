FROM python:3.11-slim

WORKDIR /app

# Устанавливаем системные зависимости
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

# Копируем зависимости
COPY requirements.txt .

# Устанавливаем Python зависимости
RUN pip install --no-cache-dir -r requirements.txt

# Создаем директорию для данных
RUN mkdir -p /app/data

# Копируем исходный код
COPY . .

# Даем права на запись в директорию данных
RUN chmod -R 777 /app/data

# Запускаем бота
CMD ["python", "-m", "bot"]