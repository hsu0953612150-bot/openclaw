# 使用 Python 3.10 輕量版
FROM python:3.10-slim

# 設定工作目錄
WORKDIR /app

# 複製依賴清單
COPY requirements.txt .

# 1. 先安裝 Python 套件（這會包含 playwright 套件）
RUN pip install --no-cache-dir -r requirements.txt

# 2. 安裝 Chromium 瀏覽器及其所需的系統環境庫 (防止跑不起來)
RUN playwright install chromium
RUN playwright install-deps chromium

# 3. 複製剩餘程式碼
COPY . .

# 啟動應用程式
CMD ["python", "app.py"]
