/**
 * NTP Clock - Synchronized time source for distributed tracing
 *
 * Provides NTP-synchronized timestamps for accurate distributed tracing.
 * Uses HTTP-based time API since browsers cannot make UDP NTP requests.
 *
 * IMPORTANT: All entities (client, server, device) MUST sync to time.cloudflare.com
 * If sync fails, timestamps will be null to indicate sync failure.
 */
class NTPClock {
    constructor() {
        this.ntpServer = 'time.cloudflare.com';  // Hardcoded - no fallback
        this.offset = null;  // Offset from local clock to NTP time (milliseconds), null if not synced
        this.lastSync = null;
        this.syncInterval = 5 * 60 * 1000;  // Re-sync every 5 minutes
        this._syncInProgress = false;
        this._syncAttempts = 0;
        this._maxSyncAttempts = 3;
    }

    /**
     * Parse Cloudflare trace response to extract timestamp
     */
    _parseCloudflareTime(text) {
        const tsMatch = text.match(/ts=([\d.]+)/);
        if (tsMatch) {
            return parseFloat(tsMatch[1]) * 1000;  // Convert to milliseconds
        }
        throw new Error('Failed to parse Cloudflare timestamp');
    }

    /**
     * Synchronize with Cloudflare NTP via HTTP
     */
    async sync() {
        if (this._syncInProgress) {
            console.log('NTP sync already in progress, skipping');
            return false;
        }

        this._syncInProgress = true;
        this._syncAttempts++;

        try {
            // Capture local time BEFORE the fetch to avoid timing drift
            const localTimeBeforeFetch = Date.now();
            const t0 = performance.now();
            const response = await fetch('https://cloudflare.com/cdn-cgi/trace');
            const t1 = performance.now();

            const text = await response.text();
            const serverTime = this._parseCloudflareTime(text);

            const latency = (t1 - t0) / 2;  // Estimate one-way latency

            // Calculate offset: server generated timestamp at local time (localTimeBeforeFetch + latency)
            // So offset = serverTime - (localTimeBeforeFetch + latency)
            this.offset = serverTime - (localTimeBeforeFetch + latency);
            this.lastSync = Date.now();

            console.log(
                `✅ NTP sync successful: offset=${this.offset.toFixed(2)}ms, ` +
                `latency=${latency.toFixed(2)}ms, server=${this.ntpServer}`
            );

            this._syncAttempts = 0;  // Reset on success
            return true;

        } catch (error) {
            console.warn(`❌ NTP sync failed (attempt ${this._syncAttempts}/${this._maxSyncAttempts}):`, error);

            // If all attempts fail, set offset to null to indicate sync failure
            if (this._syncAttempts >= this._maxSyncAttempts) {
                this.offset = null;
                this.lastSync = null;
                console.error(`⚠️  NTP sync failed after ${this._maxSyncAttempts} attempts. Timestamps will be null.`);
                this._syncAttempts = 0;
            }

            return false;
        } finally {
            this._syncInProgress = false;
        }
    }

    /**
     * Get current NTP-synchronized timestamp in milliseconds since epoch
     * Returns null if not synced
     */
    now() {
        if (this.offset === null) {
            return null;
        }
        return Date.now() + this.offset;
    }

    /**
     * Get current NTP-synchronized timestamp in ISO format
     * Returns null if not synced
     */
    nowISO() {
        const ts = this.now();
        if (ts === null) {
            return null;
        }
        return new Date(ts).toISOString();
    }

    /**
     * Get sync status for debugging
     */
    getStatus() {
        return {
            server: this.ntpServer,
            offset: this.offset,
            lastSync: this.lastSync ? new Date(this.lastSync).toISOString() : null,
            timeSinceSync: this.lastSync ? Date.now() - this.lastSync : null,
            isSynced: this.offset !== null
        };
    }

    /**
     * Start automatic periodic synchronization
     */
    startAutoSync() {
        // Initial sync
        this.sync();

        // Periodic re-sync
        setInterval(() => {
            console.log('🔄 Starting periodic NTP sync...');
            this.sync();
        }, this.syncInterval);
    }
}

// Global instance - auto-starts sync
const ntpClock = new NTPClock();
ntpClock.startAutoSync();

export default ntpClock;
