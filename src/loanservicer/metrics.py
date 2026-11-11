"""Prometheus text exposition without extra dependencies."""

from __future__ import annotations

import threading
from collections import defaultdict

BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5)


def _labels(d: dict) -> str:
    return "{" + ",".join(f'{k}="{v}"' for k, v in sorted(d.items())) + "}" if d else ""


class Metrics:
    def __init__(self):
        self.lock = threading.Lock()
        self.http = defaultdict(int)  # (route, status)
        self.payments = defaultdict(int)  # method
        self.reversals = 0
        self.payoff_quotes = 0
        self.buckets = [0] * len(BUCKETS)
        self.dur_sum = 0.0
        self.dur_count = 0
        self.gauges: dict[str, tuple[str, dict[tuple, int]]] = {}

    def observe_http(self, route: str, status: int, seconds: float) -> None:
        with self.lock:
            self.http[(route, str(status))] += 1
            for i, b in enumerate(BUCKETS):
                if seconds <= b:
                    self.buckets[i] += 1
            self.dur_sum += seconds
            self.dur_count += 1

    def set_gauge(self, name: str, help_text: str, values: dict[tuple, int]) -> None:
        """values maps a tuple of (label, value) pairs to the gauge value."""
        with self.lock:
            self.gauges[name] = (help_text, values)

    def render(self) -> str:
        out: list[str] = []
        with self.lock:
            out += [
                "# HELP ls_http_requests_total HTTP requests by route and status.",
                "# TYPE ls_http_requests_total counter",
            ]
            out += [
                f"ls_http_requests_total{_labels({'route': r, 'status': s})} {v}"
                for (r, s), v in sorted(self.http.items())
            ]
            out += [
                "# HELP ls_payments_applied_total Payments applied through the API.",
                "# TYPE ls_payments_applied_total counter",
            ]
            out += [f"ls_payments_applied_total{_labels({'method': m})} {v}" for m, v in sorted(self.payments.items())]
            out += [
                "# HELP ls_reversals_total Payment reversals through the API.",
                "# TYPE ls_reversals_total counter",
                f"ls_reversals_total {self.reversals}",
                "# HELP ls_payoff_quotes_total Payoff quotes issued.",
                "# TYPE ls_payoff_quotes_total counter",
                f"ls_payoff_quotes_total {self.payoff_quotes}",
            ]
            for name, (help_text, values) in sorted(self.gauges.items()):
                out += [f"# HELP {name} {help_text}", f"# TYPE {name} gauge"]
                out += [f"{name}{_labels(dict(k))} {v}" for k, v in sorted(values.items())]
            out += [
                "# HELP ls_request_duration_seconds Request latency.",
                "# TYPE ls_request_duration_seconds histogram",
            ]
            for b, c in zip(BUCKETS, self.buckets, strict=True):
                out.append(f'ls_request_duration_seconds_bucket{{le="{b}"}} {c}')
            out.append(f'ls_request_duration_seconds_bucket{{le="+Inf"}} {self.dur_count}')
            out.append(f"ls_request_duration_seconds_sum {self.dur_sum:.6f}")
            out.append(f"ls_request_duration_seconds_count {self.dur_count}")
        return "\n".join(out) + "\n"
