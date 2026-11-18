.DEFAULT_GOAL := help
PY ?= python
DBT := cd dbt && dbt
export DBT_SEND_ANONYMOUS_USAGE_STATS=false

help:  ## Show available commands
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install:  ## Install the project with dev dependencies
	$(PY) -m pip install -r requirements-dev.txt && $(PY) -m pip install --no-deps -e .

pipeline:  ## Run the full pipeline: generate -> ingest -> transform -> train -> score
	revintel all

generate:  ## Simulate source-system exports into data/landing
	revintel generate

ingest:  ## Validate + load landing files into the warehouse (incremental)
	revintel ingest

transform:  ## dbt build (models + tests)
	revintel transform

train:  ## Train and evaluate the churn model
	revintel train

score:  ## Score active accounts
	revintel score

api:  ## Serve the scoring API on :8000
	uvicorn revintel.api.app:app --reload --port 8000

dashboard:  ## Launch the Streamlit dashboard on :8501
	streamlit run dashboard/app.py

docs:  ## Generate and serve dbt docs (lineage graph) on :8080
	$(DBT) docs generate --profiles-dir . && dbt docs serve --profiles-dir . --port 8080

test:  ## Run unit + end-to-end tests
	$(PY) -m pytest --cov=revintel --cov-report=term-missing

lint:  ## Lint Python code
	ruff check src tests dashboard

docker:  ## Build and run everything with docker compose
	docker compose up --build

clean:  ## Remove generated data, models and dbt artifacts
	rm -rf data artifacts dbt/target dbt/logs .pytest_cache .coverage

.PHONY: help install pipeline generate ingest transform train score api dashboard docs test lint docker clean
