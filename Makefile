.PHONY: install demo seed simulate run eqsim test docker-up docker-down

install:          ## 安裝相依套件
	pip install -r requirements.txt

demo:             ## 一鍵展示（內嵌 PostgreSQL，免安裝資料庫）
	python -m scripts.demo --days 3

seed:             ## 建立主檔（需要 PostgreSQL）
	python -m scripts.seed

simulate:         ## 模擬 3 天生產資料（需要 PostgreSQL）
	python -m scripts.simulate --days 3

run:              ## 啟動 API 服務
	uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

eqsim:            ## 啟動 SECS/GEM 設備模擬器（預設埠 5001）
	python -m scripts.eqsim --port 5001 --eq-id WB-01

test:             ## 執行測試
	python -m pytest

docker-up:
	docker compose up -d --build

docker-down:
	docker compose down
