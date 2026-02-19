# Summary 

This issue occures only occasionally, but when it happens, its
  impact is very bad. The issue is active right now, where although the websocket consumer is indicating that devices are
  online and all, we are unable to communicate with any device since the 13th of the month, around two days ago. The issue is
  specifically concerning the websocket communication between the websocket consumer on the django app and devices, when this
  issue happens, it affects all connected devices.. 

# Symptoms

From a first glance, the dashboard seems to open normally, and its shows devices online as usually, almost as if everything is fine.. but when you pay attention, you'd notice that all devices are kinda disconnected. Although there is a possibility that websocket conenctions are still active, but communication is broken anyway. When we try to start a new terminal or perform any actions involving websocket communication with a device, nothing happens.

On the dashboard, the websocket connection shows the following messages (some raw details has been cropped):

Outbound: {"channel":0,"payload":{"event":"clock_sync_request","request_id":"clock_sync:1771167175307:735891"}}
Inbound: {"event": "devices", "devices": [... true list of devices with online status]}
Inbound: {"event": "clock_sync_response" ... }
Outbound: {"channel":0,"payload":{"event":"clock_sync_request","request_id":"clock_sync:1771167176451:371741"}}
Inbound: {"event": "system_info", "info": {"cpu_percent": 0.46476839869590125, "memory": {"total": 4194304000, "available": 3602509824, "percent": 14.1, "used": 591794176, "free": 2117959680, "active": 698552320, "inactive": 1156497408, "buffers": 0, "cached": 1484550144, "shared": 270336, "slab": 0}, "disk": {"total": 15786254336, "used": 4831125504, "free": 10261917696, "percent": 32.0}, "os_info": {"os_type": "Linux", "os_version": "Ubuntu 20.04 LTS", "architecture": "x86_64", "default_shell": "/bin/bash", "default_cwd": "/home/user"}, "user_context": {"username": "user", "username_source": "getpass", "home": "/home/user", "uid": 1000, "euid": 1000, "is_root": false, "has_sudo": true, "sudo_user": null, "is_sudo_session": false}, "playwright": {"installed": false, "version": null, "browsers": {}, "error": null}, "proxmox": {"is_proxmox_node": false, "version": null, "infra": {"configured": false, "network": {"applied": false, "message": null, "bridge": "vmbr1"}, "managed_containers": {"updated_at": "2026-02-15T15:04:04.945185Z", "count": 0, "total_ram_mib": 0, "total_disk_gib": 0, "total_cpu_share": 0.0, "containers": []}}}, "cloudflare_tunnel": {}, "cloudflare_forwarding": {"rules": [], "updated_at": null}, "portacode_version": "1.4.30"}, "client_sessions": ["specific..inmemory!ZVaajmIwVcJx", "specific..inmemory!mODZCxRzKVjX", "specific..inmemory!TqLBvCfVMvtY", "specific..inmemory!vlBTQgfIQZnO", "specific..inmemory!rSRQcWuAvoIF"], "reply_channel": "specific..inmemory!ZVaajmIwVcJx", "device_id": 597}
...

- The rest of the websocket communication is just back and forth clock sync, sometimes with some system_info messages as the only messages every arriving from the device side, but not at the normal frequency and not from all devices.

- The issue does not affect newly connected devices.

Below is status logs was captured from the perspective of one of the irresponsive devices while its irresponsive. It show no new logs since the 13th, but it doesn't show any errors and it claims that the service is up and apparently form the device's perspective, everything is fine. However, it's completely irresponsive. RAM and CPU usage on the device seems faily low, confirming that it is not about running out of compute.


menas@portacode-streamer:~$ portacode service status -v
[sudo] password for menas: 
Service status: active

--- system output ---
● portacode.service - Portacode persistent connection (system-wide)
     Loaded: loaded (/etc/systemd/system/portacode.service; enabled; vendor preset: enabled)
     Active: active (running) since Fri 2026-02-13 15:27:48 UTC; 1 day 23h ago
   Main PID: 1953312 (python3)
      Tasks: 6 (limit: 19020)
     Memory: 38.6M
        CPU: 2min 56.116s
     CGroup: /system.slice/portacode.service
             └─1953312 /usr/bin/python3 -m portacode connect --non-interactive

Feb 13 15:31:56 portacode-streamer python3[1953312]: [INFO] Successfully unscheduled watch for: /home/menas/portacode/docs
Feb 13 15:31:56 portacode-streamer python3[1953312]: [INFO] Successfully unscheduled watch for: /home/menas/portacode/test_modules
Feb 13 15:31:56 portacode-streamer python3[1953312]: [INFO] Successfully unscheduled watch for: /home/menas/portacode/testing_framework
Feb 13 15:31:56 portacode-streamer python3[1953312]: [INFO] Successfully unscheduled watch for: /home/menas/portacode/todo
Feb 13 15:31:56 portacode-streamer python3[1953312]: [INFO] Successfully unscheduled watch for: /home/menas/portacode/todo/issues
Feb 13 15:31:56 portacode-streamer python3[1953312]: [INFO] Successfully unscheduled watch for: /home/menas/portacode/todo/UI_UX
Feb 13 15:31:56 portacode-streamer python3[1953312]: [INFO] [DEBUG] Cleaned up project state: specific..inmemory!nhFtOFebkiHg
Feb 13 15:31:56 portacode-streamer python3[1953312]: [INFO] Cleaning up GitManager for /home/menas/portacode (session=specific..inmemory!nhFtOFebkiHg)
Feb 13 15:31:56 portacode-streamer python3[1953312]: [INFO] Stopping periodic git monitoring for /home/menas/portacode
Feb 13 15:31:56 portacode-streamer python3[1953312]: [INFO] Successfully closed GitPython repo for /home/menas/portacode

--- recent logs ---
-- Logs begin at Fri 2025-11-28 01:58:01 UTC, end at Sun 2026-02-15 14:39:59 UTC. --
Feb 13 15:31:56 portacode-streamer python3[1953312]: [INFO] Successfully unscheduled watch for: /home/menas/portacode/dist
Feb 13 15:31:56 portacode-streamer python3[1953312]: [INFO] Successfully unscheduled watch for: /home/menas/portacode/logs
Feb 13 15:31:56 portacode-streamer python3[1953312]: [INFO] Successfully unscheduled watch for: /home/menas/portacode/.pytest_cache
Feb 13 15:31:56 portacode-streamer python3[1953312]: [INFO] Successfully unscheduled watch for: /home/menas/portacode/proxmox_management
Feb 13 15:31:56 portacode-streamer python3[1953312]: [INFO] Successfully unscheduled watch for: /home/menas/portacode/tools
Feb 13 15:31:56 portacode-streamer python3[1953312]: [INFO] Successfully unscheduled watch for: /home/menas/portacode/test_results
Feb 13 15:31:56 portacode-streamer python3[1953312]: [INFO] Successfully unscheduled watch for: /home/menas/portacode/.claude
Feb 13 15:31:56 portacode-streamer python3[1953312]: [INFO] Successfully unscheduled watch for: /home/menas/portacode/examples
Feb 13 15:31:56 portacode-streamer python3[1953312]: [INFO] Successfully unscheduled watch for: /home/menas/portacode/__pycache__
Feb 13 15:31:56 portacode-streamer python3[1953312]: [INFO] Successfully unscheduled watch for: /home/menas/portacode/portacode
Feb 13 15:31:56 portacode-streamer python3[1953312]: [INFO] Successfully unscheduled watch for: /home/menas/portacode/docs
Feb 13 15:31:56 portacode-streamer python3[1953312]: [INFO] Successfully unscheduled watch for: /home/menas/portacode/test_modules
Feb 13 15:31:56 portacode-streamer python3[1953312]: [INFO] Successfully unscheduled watch for: /home/menas/portacode/testing_framework
Feb 13 15:31:56 portacode-streamer python3[1953312]: [INFO] Successfully unscheduled watch for: /home/menas/portacode/todo
Feb 13 15:31:56 portacode-streamer python3[1953312]: [INFO] Successfully unscheduled watch for: /home/menas/portacode/todo/issues
Feb 13 15:31:56 portacode-streamer python3[1953312]: [INFO] Successfully unscheduled watch for: /home/menas/portacode/todo/UI_UX
Feb 13 15:31:56 portacode-streamer python3[1953312]: [INFO] [DEBUG] Cleaned up project state: specific..inmemory!nhFtOFebkiHg
Feb 13 15:31:56 portacode-streamer python3[1953312]: [INFO] Cleaning up GitManager for /home/menas/portacode (session=specific..inmemory!nhFtOFebkiHg)
Feb 13 15:31:56 portacode-streamer python3[1953312]: [INFO] Stopping periodic git monitoring for /home/menas/portacode
Feb 13 15:31:56 portacode-streamer python3[1953312]: [INFO] Successfully closed GitPython repo for /home/menas/portacode

menas@portacode-streamer:~$ 


- Any irresponsive device becomes responsive again as soon as we stop/start the portacode service, or if we restart the django server, that restores the connection for all devices.. But the main issue is not about how to restore it, but rather about why the device doesn't detect when its own connection is dead.

# Most important finding:

When this issue is active, the server logs look something like this:
Invalid or disconnected target session: specific..inmemory!ZVaajmIwVcJx
Invalid or disconnected target session: specific..inmemory!mODZCxRzKVjX
Invalid or disconnected target session: specific..inmemory!TqLBvCfVMvtY
Invalid or disconnected target session: specific..inmemory!ZVaajmIwVcJx
Invalid or disconnected target session: specific..inmemory!mODZCxRzKVjX
Invalid or disconnected target session: specific..inmemory!TqLBvCfVMvtY
Invalid or disconnected target session: specific..inmemory!ZVaajmIwVcJx
Invalid or disconnected target session: specific..inmemory!mODZCxRzKVjX
Invalid or disconnected target session: specific..inmemory!TqLBvCfVMvtY

This error should almost never happen, especially not so consistently. It may happen very briefly on race conditions when a device gets disconnected while the server is about to route a message to it.

Another issue that has been noticed to happen ocasionally on the client side is that sometimes the server does the same thing with the websocket connection of the client and the client side shows error about failing to connect, even though its websocket connection has successfully been established, but might have failed to be added to the global variable mapping connected devices causing the server to refuse to route its messages and making the connection effectively useless even though it is in fact active. 