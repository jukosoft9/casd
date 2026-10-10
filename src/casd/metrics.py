"""A minimal Prometheus text-format exporter (counters, gauges, one histogram)."""

from __future__ import annotations

import threading
from typing import Callable

BUCKETS = (0.001, 0.005, 0.025, 0.1, 0.5, 2.5, 10.0)

_HELP = {
    "casd_requests_total": "Requests by method, kind (ac/cas) and result.",
    "casd_received_bytes_total": "Bytes accepted in PUT bodies.",
    "casd_sent_bytes_total": "Bytes sent in GET bodies.",
    "casd_evictions_total": "Entries removed to stay under the size limit.",
    "casd_evicted_bytes_total": "Bytes removed to stay under the size limit.",
    "casd_ac_invalidated_total": "Action results dropped because a referenced blob was gone.",
}


def _labels(pairs: tuple[tuple[str, str], ...]) -> str:
    if not pairs:
        return ""
    return "{" + ",".join(f'{k}="{v}"' for k, v in pairs) + "}"


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, dict[tuple, float]] = {name: {} for name in _HELP}
        self._durations: dict[str, list[float]] = {}
        self._gauges: list[tuple[str, str, Callable[[], float]]] = []

    def inc(self, name: str, amount: float = 1, **labels: str) -> None:
        key = tuple(sorted(labels.items()))
        with self._lock:
            series = self._counters[name]
            series[key] = series.get(key, 0) + amount

    def observe(self, method: str, seconds: float) -> None:
        with self._lock:
            row = self._durations.setdefault(method, [0.0] * (len(BUCKETS) + 2))
            for i, bound in enumerate(BUCKETS):
                if seconds <= bound:
                    row[i] += 1
            row[-2] += seconds
            row[-1] += 1

    def gauge(self, name: str, help_text: str, read: Callable[[], float]) -> None:
        self._gauges.append((name, help_text, read))

    def get(self, name: str, **labels: str) -> float:
        with self._lock:
            return self._counters[name].get(tuple(sorted(labels.items())), 0)

    def render(self) -> str:
        lines: list[str] = []
        with self._lock:
            for name, series in self._counters.items():
                lines.append(f"# HELP {name} {_HELP[name]}")
                lines.append(f"# TYPE {name} counter")
                for key, value in sorted(series.items()) or [((), 0)]:
                    lines.append(f"{name}{_labels(key)} {value:g}")
            name = "casd_request_duration_seconds"
            lines.append(f"# HELP {name} Time to handle a request.")
            lines.append(f"# TYPE {name} histogram")
            for method, row in sorted(self._durations.items()):
                for bound, count in zip(BUCKETS, row):
                    lines.append(f'{name}_bucket{{method="{method}",le="{bound:g}"}} {count:g}')
                lines.append(f'{name}_bucket{{method="{method}",le="+Inf"}} {row[-1]:g}')
                lines.append(f'{name}_sum{{method="{method}"}} {row[-2]:.6f}')
                lines.append(f'{name}_count{{method="{method}"}} {row[-1]:g}')
        for name, help_text, read in self._gauges:
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} gauge")
            lines.append(f"{name} {read():g}")
        return "\n".join(lines) + "\n"
