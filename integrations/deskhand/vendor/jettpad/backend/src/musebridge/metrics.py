"""Bounded monotonic duration samples; never records user content."""
from collections import defaultdict, deque


class Metrics:
    def __init__(self, sample_count=256):
        self.samples = defaultdict(lambda: deque(maxlen=sample_count))
        self.counts = defaultdict(int)

    def observe(self, operation: str, elapsed: float):
        if len(self.samples) >= 64 and operation not in self.samples:
            operation = "other"
        self.samples[operation].append(max(0, elapsed) * 1000)
        self.counts[operation] += 1

    def snapshot(self):
        result = {}
        for operation, values in self.samples.items():
            ordered = sorted(values)
            result[operation] = {"count": self.counts[operation], "samples": len(ordered),
                                 "p50_ms": round(ordered[len(ordered)//2], 3),
                                 "p95_ms": round(ordered[min(len(ordered)-1, int(len(ordered)*.95))], 3)}
        return result
