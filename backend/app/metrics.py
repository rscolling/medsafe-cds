"""Prometheus metrics (own registry so tests can create isolated apps)."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram


class Metrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.http_requests = Counter(
            "medsafe_http_requests_total",
            "HTTP requests",
            ["method", "path", "status"],
            registry=self.registry,
        )
        self.http_latency = Histogram(
            "medsafe_http_request_seconds",
            "HTTP latency",
            ["path"],
            registry=self.registry,
            buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
        )
        self.alerts = Counter(
            "medsafe_alerts_total",
            "Cards returned",
            ["rule", "mode", "source", "kind"],
            registry=self.registry,
        )
        self.suppressed = Counter(
            "medsafe_alerts_suppressed_total",
            "Baseline alerts suppressed by context",
            ["rule", "source"],
            registry=self.registry,
        )
        self.source_errors = Counter(
            "medsafe_source_errors_total",
            "Data source failures (fail-open)",
            ["source"],
            registry=self.registry,
        )
        self.rate_limited = Counter("medsafe_rate_limited_total", "429 responses", registry=self.registry)
        self.overrides = Counter(
            "medsafe_overrides_total", "Card feedback outcomes", ["outcome"], registry=self.registry
        )
        self.rules_loaded = Gauge("medsafe_rules_loaded", "Rules loaded", registry=self.registry)
