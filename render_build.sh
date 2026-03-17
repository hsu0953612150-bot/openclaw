#!/usr/bin/env bash
# 更新環境並安裝套件
pip install --upgrade pip
pip install -r requirements.txt

# 安裝 Playwright 核心瀏覽器 (只裝 chromium 以節省空間)
playwright install chromium
playwright install-deps chromium
