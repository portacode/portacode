"""
NTP Clock - Synchronized time source for distributed tracing

Provides NTP-synchronized timestamps for accurate distributed tracing.
Thread-safe implementation with automatic periodic synchronization.

IMPORTANT: All entities (client, server, device) MUST sync to time.cloudflare.com
If sync fails, timestamps will be None to indicate sync failure.
"""
import ntplib
import time
import threading
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


class NTPClock:
    """Thread-safe NTP-synchronized clock."""

    def __init__(self, ntp_server: str = 'time.cloudflare.com'):
        """Initialize NTP clock.

        Args:
            ntp_server: NTP server hostname (default: time.cloudflare.com, hardcoded, no fallback)
        """
        self.ntp_server = ntp_server
        self.offset: Optional[float] = None  # Offset from local clock to NTP time (seconds), None if not synced
        self.last_sync: Optional[float] = None
        self.sync_interval = 300  # Re-sync every 5 minutes
        self._lock = threading.Lock()
        self._sync_in_progress = False
        self._client = ntplib.NTPClient()
        self._sync_attempts = 0
        self._max_sync_attempts = 3

    def sync(self) -> bool:
        """Synchronize with NTP server.

        Returns:
            True if sync successful, False otherwise
        """
        if self._sync_in_progress:
            logger.debug("NTP sync already in progress, skipping")
            return False

        self._sync_in_progress = True

        try:
            self._sync_attempts += 1
            response = self._client.request(self.ntp_server, version=3, timeout=2)

            with self._lock:
                # Offset is difference between NTP time and local time
                self.offset = response.offset
                self.last_sync = time.time()

            logger.info(
                f"✅ NTP sync successful: offset={self.offset*1000:.2f}ms, "
                f"latency={response.delay*1000:.2f}ms, server={self.ntp_server}"
            )

            self._sync_attempts = 0  # Reset on success
            return True

        except Exception as e:
            logger.warning(f"❌ NTP sync failed (attempt {self._sync_attempts}/{self._max_sync_attempts}): {e}")

            # If all attempts fail, set offset to None to indicate sync failure
            if self._sync_attempts >= self._max_sync_attempts:
                with self._lock:
                    self.offset = None
                    self.last_sync = None
                logger.error(f"⚠️  NTP sync failed after {self._max_sync_attempts} attempts. Timestamps will be None.")
                self._sync_attempts = 0

            return False

        finally:
            self._sync_in_progress = False

    def now(self) -> Optional[float]:
        """Get current NTP-synchronized timestamp (seconds since epoch).

        Returns:
            Timestamp in seconds (Unix epoch) or None if not synced
        """
        with self._lock:
            if self.offset is None:
                return None
            return time.time() + self.offset

    def now_ms(self) -> Optional[int]:
        """Get current NTP-synchronized timestamp in milliseconds.

        Returns:
            Timestamp in milliseconds (Unix epoch) or None if not synced
        """
        ts = self.now()
        if ts is None:
            return None
        return int(ts * 1000)

    def now_iso(self) -> Optional[str]:
        """Get current NTP-synchronized timestamp in ISO format.

        Returns:
            ISO 8601 formatted timestamp with UTC timezone or None if not synced
        """
        ts = self.now()
        if ts is None:
            return None
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return dt.isoformat()

    def get_status(self) -> dict:
        """Get sync status for debugging.

        Returns:
            Dictionary with sync status information
        """
        with self._lock:
            return {
                'server': self.ntp_server,
                'offset_ms': self.offset * 1000 if self.offset is not None else None,
                'last_sync': datetime.fromtimestamp(self.last_sync, tz=timezone.utc).isoformat() if self.last_sync else None,
                'time_since_sync_sec': time.time() - self.last_sync if self.last_sync else None,
                'is_synced': self.offset is not None
            }

    def start_auto_sync(self):
        """Start automatic periodic synchronization in background thread."""
        # Initial sync
        self.sync()

        def _sync_loop():
            while True:
                time.sleep(self.sync_interval)
                logger.info("🔄 Starting periodic NTP sync...")
                self.sync()

        thread = threading.Thread(target=_sync_loop, daemon=True, name='ntp-sync')
        thread.start()
        logger.info(f"Started NTP auto-sync thread (interval: {self.sync_interval}s, server: {self.ntp_server})")


# Global instance - auto-starts sync on import
ntp_clock = NTPClock()
ntp_clock.start_auto_sync()
