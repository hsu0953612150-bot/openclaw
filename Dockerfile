FROM python:3.10-slim

WORKDIR /app

# 複製依賴清單
COPY requirements.txt .

# 1. 先安裝 Python 套件
RUN pip install --no-cache-dir -r requirements.txt

# 2. 安裝 Playwright 及其系統依賴 (確保視覺模組正常)
RUN playwright install chromium
RUN playwright install-deps chromium

# 3. 複製其餘程式碼
COPY . .

# 使用生產級伺服器啟動
CMD ["python", "app.py"]
