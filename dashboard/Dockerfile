FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8777

CMD ["gunicorn", "-b", "0.0.0.0:8777", "-w", "1", "--threads", "2", "server:app"]
