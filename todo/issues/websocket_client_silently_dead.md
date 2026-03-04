# Issue summary

The SDK is supposed to keep trying to reconnect whenever the websocket connection to the server is dead, and that is what it do most of the time, but in some rare instances, the connection drops in a way where the server shows the the device is offline, but the device logs show that the SDK assums that it is connected. Result is that a manual restart is needed as the device SDK never detects that is it offline/disconnected so the reconnect loop never triggers.

# Issue details and findings

• Most likely root cause: the client got stuck in a half-closed TCP state (CLOSE-WAIT) where the websocket reader stopped consuming bytes, so reconnect logic never
  triggered.

  What I found (read-only):

  1. Live process is still running since 2026-03-04 08:15:27 UTC with no restart.
  2. Last Portacode log line is 2026-03-04 08:28:58 UTC; no logs after that.
  3. The only Portacode network socket is:
      - 10.10.0.136:41718 -> 94.204.184.54:443 in CLOSE-WAIT
      - kernel Recv-Q is 177 bytes (unread data still queued)
      - FD is 6 in process 7061
  4. Restart policy is Restart=on-failure, but this process never exits, so systemd won’t restart it.

  Code correlation (installed portacode==1.4.35 matches tag v1.4.35 exactly):

  1. Receive loop only reconnects when recv() raises websockets.ConnectionClosed:
      - /home/user/debo/portacode/portacode/connection/client.py:172:172
      - /home/user/debo/portacode/portacode/connection/client.py:203:203
  2. It wraps recv() in asyncio.wait_for(..., timeout=1.0) and cancels it every second on idle:
      - /home/user/debo/portacode/portacode/connection/client.py:176:176
  3. Clock-sync-based forced close is only initial burst (5 times), not continuous:
      - /home/user/debo/portacode/portacode/connection/client.py:231:231

  Inference:

  - You hit a rare wedge where the websocket path stopped draining/closing after peer-side close (or close frame), leaving FD in CLOSE-WAIT.
  - Because the process didn’t crash and no close exception propagated, reconnect loop never ran, and systemd didn’t intervene.
  - Limited ongoing heartbeat (only initial clock sync) removes a second safety net once this wedge happens.

  Confidence: medium-high on “stuck reader in CLOSE-WAIT without reconnect”; medium on exact trigger being the per-second wait_for(recv) cancellation race vs a lower-level
  websockets/SSL edge case.

# Suggestions

1. WebSocket protocol keepalive (Ping/Pong)
     Enable client-side idle ping at a low frequency (for example every 60-180s only when no traffic). This uses WebSocket control frames, not JSON payloads, and a
     compliant server stack auto-replies with Pong without custom server code. If pong is missed, client force-closes and reconnects.
2. TCP keepalive probes
     Enable and tune socket keepalive (SO_KEEPALIVE, TCP_KEEPIDLE, TCP_KEEPINTVL, TCP_KEEPCNT) on the client connection. This is kernel-level liveness detection with
     minimal overhead and no app-channel traffic. If peer becomes unreachable/half-dead, TCP eventually errors, allowing client reconnect logic to trigger.


# Resolution

Suggestion 1 has been implemented in the commit on which this issue file was added and the issue file is kept to further monitor the issue in case of any future reoccurence. The file may be removed in 3 month later if the issue never reoccured. Scheduled file detetion is 4th of Jun 2026