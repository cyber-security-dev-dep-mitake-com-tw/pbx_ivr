from prometheus_client import Counter, Gauge, Histogram

REQUEST_COUNT = Counter(
    "ht812_api_requests_total",
    "Total API requests by endpoint",
    ["endpoint"],
)

REQUEST_LATENCY = Histogram(
    "ht812_api_request_duration_seconds",
    "API request latency in seconds",
    ["endpoint"],
    buckets=(0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

LOGIN_FAILURES = Counter(
    "ht812_login_failures_total",
    "Total HT812 authentication failures",
)

BACKUP_FILE_COUNT = Gauge(
    "ht812_backup_files_total",
    "Number of backup XML files currently stored",
)


def record_login_failure() -> None:
    LOGIN_FAILURES.inc()
