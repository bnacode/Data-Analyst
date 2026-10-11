"""Runs guarded SQL on BigQuery, capped in cost and time."""

from __future__ import annotations

import pandas as pd
from google.cloud import bigquery


class BigQueryRunner:
    """Runs SQL on BigQuery with a bytes-billed cap and a timeout."""

    def __init__(self, project: str, max_bytes_billed: int, timeout_s: int) -> None:
        self._client = bigquery.Client(project=project)
        self._config = bigquery.QueryJobConfig(maximum_bytes_billed=max_bytes_billed, job_timeout_ms=timeout_s * 1000)
        self._timeout_s = timeout_s

    def run(self, sql: str) -> pd.DataFrame:
        job = self._client.query(sql, job_config=self._config)
        return job.result(timeout=self._timeout_s).to_dataframe(create_bqstorage_client=False)
