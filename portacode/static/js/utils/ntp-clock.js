/**
 * NTP Clock - Synchronized time source for distributed tracing
 *
 * Provides synchronization by requesting the server clock across the WebSocket
 * channel defined in `WEBSOCKET_PROTOCOL`. The client measures round-trip time
 * and applies an offset that compensates for latency before exposing timestamps.
 */
class NTPClock {
    constructor() {
        this.ntpServer = 'portacode-gateway';
        this.offset = null;
        this.lastSync = null;
        this.lastLatencyMs = null;
        this.syncInterval = 60 * 1000;
        this.clockSyncTimeout = 20 * 1000;
        this.maxSyncFailures = 3;

        this._clockSyncSender = null;
        this._failureCallback = null;
        this._clockSyncTimer = null;
        this._clockSyncTimeoutHandle = null;
        this._pendingRequest = null;
        this._autoSyncStarted = false;
        this._clockSyncFailures = 0;
    }

    /**
     * Register the function responsible for piping `clock_sync_request`
     * packets over the dashboard WebSocket.
     */
    setClockSyncSender(sender) {
        this._clockSyncSender = sender;
        if (this._autoSyncStarted && !this._pendingRequest) {
            this._scheduleSync(0);
        }
    }

    /**
     * Callback invoked when multiple sync failures occur in a row.
     * Useful for triggering a transport reconnect.
     */
    onClockSyncFailure(callback) {
        this._failureCallback = callback;
    }

    /**
     * Start the auto-sync loop (idempotent). Will skip sending until a sender
     * has been registered.
     */
    startAutoSync() {
        if (this._autoSyncStarted) {
            return;
        }
        this._autoSyncStarted = true;
        this._scheduleSync(0);
    }

    /**
     * Stop the auto-sync loop (used primarily in tests).
     */
    stopAutoSync() {
        this._autoSyncStarted = false;
        if (this._clockSyncTimer) {
            clearTimeout(this._clockSyncTimer);
            this._clockSyncTimer = null;
        }
        this._clearPendingRequest();
    }

    sync() {
        return this._performClockSync();
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

    handleServerSync(payload) {
        if (!payload) {
            return;
        }
        const receiveTime = Date.now();
        let roundTrip = 0;
        if (
            payload.request_id &&
            this._pendingRequest &&
            payload.request_id === this._pendingRequest.requestId
        ) {
            roundTrip = Math.max(receiveTime - this._pendingRequest.sentAt, 0);
            this._clockSyncFailures = 0;
            this._clearPendingRequest();
        }
        const serverSend = typeof payload.server_send_time === 'number' ? payload.server_send_time : payload.server_time;
        const serverReceive = payload.server_receive_time;
        const serverAvg = (typeof serverReceive === 'number' && typeof serverSend === 'number')
            ? (serverReceive + serverSend) / 2
            : typeof serverSend === 'number'
                ? serverSend
                : undefined;
        if (typeof serverAvg === 'number') {
            this.updateFromServer(serverAvg, roundTrip);
        }
        if (payload.server_time_iso) {
            this.serverTimeIso = payload.server_time_iso;
        }
        this._scheduleSync(this.syncInterval);
    }

    applyServerSync(payload) {
        this.handleServerSync(payload);
    }

    updateFromServer(serverTimeMs, roundTripMs = 0) {
        const receiveTime = Date.now();
        const halfLatency = (roundTripMs || 0) / 2;
        const estimatedServerReceived = receiveTime - halfLatency;
        this.offset = serverTimeMs - estimatedServerReceived;
        this.lastLatencyMs = roundTripMs;
        this.lastSync = receiveTime;
    }

    getStatus() {
        return {
            server: this.ntpServer,
            offset: this.offset,
            lastSync: this.lastSync ? new Date(this.lastSync).toISOString() : null,
            lastLatencyMs: this.lastLatencyMs,
            timeSinceSync: this.lastSync ? Date.now() - this.lastSync : null,
            isSynced: this.offset !== null
        };
    }

    _scheduleSync(delay) {
        if (!this._autoSyncStarted) {
            return;
        }
        if (this._clockSyncTimer) {
            clearTimeout(this._clockSyncTimer);
        }
        this._clockSyncTimer = setTimeout(() => this._performClockSync(), delay);
    }

    _performClockSync() {
        if (!this._autoSyncStarted) {
            return false;
        }
        if (!this._clockSyncSender) {
            this._scheduleSync(Math.min(this.syncInterval, 3000));
            return false;
        }
        if (this._pendingRequest) {
            return false;
        }
        const requestId = this._generateRequestId();
        const payload = {
            event: 'clock_sync_request',
            request_id: requestId,
        };
        this._pendingRequest = {
            requestId,
            sentAt: Date.now(),
        };
        const sent = this._clockSyncSender(payload);
        if (!sent) {
            this._handleClockSyncFailure();
            return false;
        }
        this._clockSyncTimeoutHandle = setTimeout(() => this._handleClockSyncTimeout(), this.clockSyncTimeout);
        return true;
    }

    _handleClockSyncTimeout() {
        this._clockSyncFailures += 1;
        this._clearPendingRequest();
        if (
            this._clockSyncFailures >= this.maxSyncFailures &&
            typeof this._failureCallback === 'function'
        ) {
            this._failureCallback();
        }
        this._scheduleSync(this.syncInterval);
    }

    _handleClockSyncFailure() {
        this._clockSyncFailures += 1;
        this._clearPendingRequest();
        if (
            this._clockSyncFailures >= this.maxSyncFailures &&
            typeof this._failureCallback === 'function'
        ) {
            this._failureCallback();
        }
        this._scheduleSync(this.syncInterval);
    }

    _clearPendingRequest() {
        if (this._clockSyncTimeoutHandle) {
            clearTimeout(this._clockSyncTimeoutHandle);
            this._clockSyncTimeoutHandle = null;
        }
        this._pendingRequest = null;
    }

    _generateRequestId() {
        return `clock_sync:${Date.now()}:${Math.floor(Math.random() * 1000000)}`;
    }
}

// Global instance - auto-starts sync
const ntpClock = new NTPClock();
ntpClock.startAutoSync();

export default ntpClock;
