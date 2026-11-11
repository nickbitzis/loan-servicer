PORT ?= 8880

.PHONY: install test lint format run simulate gate screenshot docker-build docker-run clean

install:
	uv sync --python 3.12

test:
	uv run --python 3.12 --group dev pytest -q

lint:
	uvx ruff check .
	uvx ruff format --check .

format:
	uvx ruff format .
	uvx ruff check . --fix

run:
	PORT=$(PORT) uv run loanservicer serve

simulate:  ## 24 months, 5,000 loans, full report
	uv run loanservicer simulate --months 24

gate:  ## what CI runs: 12 months, exit 1 unless every invariant holds and remittance ties out
	uv run loanservicer simulate --months 12 --strict

screenshot:  ## needs the server running on $(PORT) and Google Chrome on macOS
	"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new --disable-gpu --hide-scrollbars \
		--force-device-scale-factor=1 --blink-settings=preferredColorScheme=1 --window-size=1400,1900 \
		--virtual-time-budget=8000 --screenshot=docs/screenshot.png "http://127.0.0.1:$(PORT)/?loan=TW-100004"

docker-build:
	docker build -t loan-servicer .

docker-run:
	docker run --rm -p $(PORT):8880 loan-servicer

clean:
	rm -rf .pytest_cache .ruff_cache var out
