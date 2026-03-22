FROM python:3.10-slim

# 安裝基礎系統依賴
RUN apt-get update && apt-get install -y \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先安裝 Python 套件
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --- 關鍵修復：使用 python -m 呼叫 playwright ---
RUN python -m playwright install chromium
RUN python -m playwright install-deps chromium

COPY . .

# Render 預設使用 PORT 1000
EXPOSE 1000

CMD ["python", "app.py"]
