"""
Simple clock offset helper that tracks the difference between the local clock and
the server time. The device obtains the server time over the existing gateway
WebSocket and updates the offset based on measured round-trip time.
"""
import time
from datetime import datetime, timezone
from typing import Optional


class NTPClock:
    """Clock helper that stores a millisecond offset from the local system clock."""

    def __init__(self):
        self._offset_ms = 0.0
        self._last_sync = None
        self._last_latency_ms = None
        self._smoothing_weight = 0.2

    def now(self) -> float:
        """Return the current timestamp (seconds since epoch) adjusted by the offset."""
        return time.time() + (self._offset_ms / 1000.0)

    def now_ms(self) -> int:
        """Return the current timestamp in milliseconds adjusted by the offset."""
        return int(time.time() * 1000 + self._offset_ms)

    def now_iso(self) -> str:
        """Return the current ISO timestamp adjusted by the offset."""
        return datetime.fromtimestamp(self.now(), tz=timezone.utc).isoformat()

    def get_status(self) -> dict:
        """Return metadata about the last synchronization."""
        return {
            'server': 'gateway',
            'offset_ms': self._offset_ms,
            'last_sync': datetime.fromtimestamp(self._last_sync, tz=timezone.utc).isoformat() if self._last_sync else None,
            'last_latency_ms': self._last_latency_ms,
            'is_synced': self._last_sync is not None,
        }

    def update_from_server(self, server_time_ms: float, latency_ms: float) -> None:
        """Update the clock offset using the server timestamp and measured latency."""
        client_receive_ms = time.time() * 1000
        half_latency = latency_ms / 2
        estimated_server_received = client_receive_ms - half_latency
        new_offset = server_time_ms - estimated_server_received
        if self._last_sync is not None:
            self._offset_ms += self._smoothing_weight * (new_offset - self._offset_ms)
        else:
            self._offset_ms = new_offset
        self._last_latency_ms = latency_ms
        self._last_sync = time.time()

    def sync(self) -> bool:
        """Legacy sync stub kept for compatibility; no-op so callers continue to work."""
        return False

    def start_auto_sync(self):
        """Legacy stub that does nothing now that sync happens via the gateway."""
        pass


# Global instance
ntp_clock = NTPClock()
