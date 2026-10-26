# Contributing

Use Python 3.12 and keep changes focused. Include a reproducible issue or a clear explanation
of the behavior being changed. Never include `.env`, real credentials, or personal trading data.

## Development setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
make check
make build
```

`make check` runs Ruff, strict Mypy, and pytest. The test suite uses fixtures and mocks for
external market APIs. Ordinary tests do not require Docker containers or exchange credentials.
Compose configuration tests require the Compose plugin but do not start containers.

## PostgreSQL integration tests

Use a disposable PostgreSQL 17 database, separate from the application database. For example:

```bash
docker run --rm -d --name prediction-trader-test-db \
  -p 127.0.0.1:15432:5432 \
  -e POSTGRES_USER=prediction_test \
  -e POSTGRES_PASSWORD=test-only-password \
  -e POSTGRES_DB=prediction_test postgres:17-alpine
docker exec prediction-trader-test-db pg_isready -U prediction_test -d prediction_test
```

Wait for `pg_isready` to report that the database accepts connections, then run:

```bash
PREDICTION_TEST_DATABASE_URL='postgresql+psycopg://prediction_test:test-only-password@127.0.0.1:15432/prediction_test' \
  python -m pytest -q
docker stop prediction-trader-test-db
```

Each database test creates and drops its own UUID-named schema. Do not supply the production
database URL. Without `PREDICTION_TEST_DATABASE_URL`, those tests are explicitly skipped.

## Browser interaction tests

Point `DASHBOARD_CHROME_BINARY` to an installed Chrome or Chromium executable:

```bash
DASHBOARD_CHROME_BINARY="$(command -v google-chrome)" \
  python -m pytest -q tests/test_indicator_page_browser.py
```

The browser opens a temporary HTML fixture, intercepts market requests, and uses simulated
orders only. It does not open the running dashboard. Windows Chrome launched from WSL also
requires `TMPDIR` to point to a Windows-accessible directory under `/mnt/c`.

## CI

The `CI` workflow runs for pushes, pull requests and manual dispatches on Ubuntu with Python 3.12.
It provisions a disposable PostgreSQL service and uses Chrome from the hosted runner. It checks:

- Ruff and Mypy;
- unit, PostgreSQL and browser tests;
- a fresh `./pt init` followed by portable Compose validation;
- source/wheel builds and dependency metadata;
- a Docker image build and CLI smoke test, without starting market collectors.

CI has read-only repository permissions and uses no deployment or exchange secrets. The pinned
action revisions are annotated with their release versions so they can be updated deliberately.

## Scope and behavior

Keep the default deployment paper-only. Strategies emit signals; execution decisions stay at
their documented boundaries. Deployment changes must preserve existing data volumes and offer
an explicit path for workstation-specific options. See [architecture](docs/architecture.md)
and [installation](docs/installation.md).

For a pull request, describe the change and include the checks you actually ran, including skips.
The repository does not currently specify an open-source license.
