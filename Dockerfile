FROM python:3.10-slim

WORKDIR /app

# 複製依賴文件
COPY requirements.txt .

# 1. 先安裝 Python 套件
RUN pip install --no-cache-dir -r requirements.txt

# 2. 安裝 Playwright 瀏覽器及其依賴 (GPT-4o 視覺與搜尋備用)
RUN playwright install chromium
RUN playwright install-deps chromium

# 3. 複製其餘程式碼
COPY . .

CMD ["python", "app.py"]
