# 使用官方預裝好瀏覽器環境的映像檔
FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

# 設定程式在容器內的工作目錄
WORKDIR /app

# 1. 複製依賴清單並安裝 Python 套件
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 2. 下載 Chromium 瀏覽器本體
RUN playwright install chromium

# 3. 複製其餘所有程式碼（app.py 等）
COPY . .

# 設定 Render 監聽的連接埠
ENV PORT=10000
EXPOSE 10000

# 啟動應用程式
CMD ["python", "app.py"]
