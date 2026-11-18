FROM python:3.13-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DBT_SEND_ANONYMOUS_USAGE_STATS=false

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-deps -e .

COPY config ./config
COPY dbt ./dbt
COPY dashboard ./dashboard

RUN useradd --create-home appuser && mkdir -p data artifacts && chown -R appuser /app
USER appuser

EXPOSE 8000 8501
CMD ["revintel", "all"]
