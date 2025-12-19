

Frequency of reoccurence: Rare
Importance: Critical

The issue has been happening with recent versions of portacode, probably after adding handlers for search_project_files and read_project_file where the service will be running and the command "portacode service status" shows "Service status: active" and in verbose mode, here's the output:

menas@portacode-streamer:~/portacode$ portacode service status -v
Service status: active

--- system output ---
● portacode.service - Portacode persistent connection (system-wide)
     Loaded: loaded (/etc/systemd/system/portacode.service; enabled; vendor preset: enabled)
     Active: active (running) since Tue 2025-10-28 17:20:26 UTC; 16h ago
   Main PID: 1123848 (python3)
      Tasks: 53 (limit: 19020)
     Memory: 30.7M
        CPU: 1h 19min 54.804s
     CGroup: /system.slice/portacode.service
             ├─1123848 /usr/bin/python3 -m portacode connect --non-interactive
             ├─1124026 git cat-file --batch-check
             ├─1124106 git cat-file --batch-check
             ├─1157407 git cat-file --batch-check
             ├─1159306 git cat-file --batch-check
             ├─1160451 git cat-file --batch-check
             ├─1162408 git cat-file --batch-check
             ├─1164471 git cat-file --batch-check
             ├─1181608 git cat-file --batch-check
             ├─1192855 git cat-file --batch-check
             └─1211883 git cat-file --batch-check

Oct 29 09:20:43 portacode-streamer python3[1123848]: [INFO] 🔄 Starting periodic NTP sync...
Oct 29 09:20:43 portacode-streamer python3[1123848]: [INFO] ✅ NTP sync successful: offset=4.18ms, latency=7.25ms, server=time.cloudflare.com
Oct 29 09:25:43 portacode-streamer python3[1123848]: [INFO] 🔄 Starting periodic NTP sync...
Oct 29 09:25:43 portacode-streamer python3[1123848]: [INFO] ✅ NTP sync successful: offset=3.51ms, latency=6.30ms, server=time.cloudflare.com
Oct 29 09:30:43 portacode-streamer python3[1123848]: [INFO] 🔄 Starting periodic NTP sync...
Oct 29 09:30:43 portacode-streamer python3[1123848]: [INFO] ✅ NTP sync successful: offset=2.85ms, latency=7.06ms, server=time.cloudflare.com
Oct 29 09:36:40 portacode-streamer python3[1123848]: [INFO] 🔄 Starting periodic NTP sync...
Oct 29 09:37:59 portacode-streamer python3[1123848]: [INFO] ✅ NTP sync successful: offset=10.93ms, latency=15.25ms, server=time.cloudflare.com
Oct 29 09:42:58 portacode-streamer python3[1123848]: [INFO] 🔄 Starting periodic NTP sync...
Oct 29 09:42:58 portacode-streamer python3[1123848]: [INFO] ✅ NTP sync successful: offset=7.10ms, latency=5.28ms, server=time.cloudflare.com

--- recent logs ---
-- Logs begin at Thu 2025-10-02 13:56:04 UTC, end at Wed 2025-10-29 09:43:24 UTC. --
Oct 29 08:55:43 portacode-streamer python3[1123848]: [INFO] 🔄 Starting periodic NTP sync...
Oct 29 08:55:43 portacode-streamer python3[1123848]: [INFO] ✅ NTP sync successful: offset=7.12ms, latency=9.52ms, server=time.cloudflare.com
Oct 29 09:00:43 portacode-streamer python3[1123848]: [INFO] 🔄 Starting periodic NTP sync...
Oct 29 09:00:43 portacode-streamer python3[1123848]: [INFO] ✅ NTP sync successful: offset=4.70ms, latency=5.65ms, server=time.cloudflare.com
Oct 29 09:05:43 portacode-streamer python3[1123848]: [INFO] 🔄 Starting periodic NTP sync...
Oct 29 09:05:43 portacode-streamer python3[1123848]: [INFO] ✅ NTP sync successful: offset=4.28ms, latency=6.05ms, server=time.cloudflare.com
Oct 29 09:10:43 portacode-streamer python3[1123848]: [INFO] 🔄 Starting periodic NTP sync...
Oct 29 09:10:43 portacode-streamer python3[1123848]: [INFO] ✅ NTP sync successful: offset=1.89ms, latency=6.13ms, server=time.cloudflare.com
Oct 29 09:15:43 portacode-streamer python3[1123848]: [INFO] 🔄 Starting periodic NTP sync...
Oct 29 09:15:43 portacode-streamer python3[1123848]: [INFO] ✅ NTP sync successful: offset=0.81ms, latency=6.46ms, server=time.cloudflare.com
Oct 29 09:20:43 portacode-streamer python3[1123848]: [INFO] 🔄 Starting periodic NTP sync...
Oct 29 09:20:43 portacode-streamer python3[1123848]: [INFO] ✅ NTP sync successful: offset=4.18ms, latency=7.25ms, server=time.cloudflare.com
Oct 29 09:25:43 portacode-streamer python3[1123848]: [INFO] 🔄 Starting periodic NTP sync...
Oct 29 09:25:43 portacode-streamer python3[1123848]: [INFO] ✅ NTP sync successful: offset=3.51ms, latency=6.30ms, server=time.cloudflare.com
Oct 29 09:30:43 portacode-streamer python3[1123848]: [INFO] 🔄 Starting periodic NTP sync...
Oct 29 09:30:43 portacode-streamer python3[1123848]: [INFO] ✅ NTP sync successful: offset=2.85ms, latency=7.06ms, server=time.cloudflare.com
Oct 29 09:36:40 portacode-streamer python3[1123848]: [INFO] 🔄 Starting periodic NTP sync...
Oct 29 09:37:59 portacode-streamer python3[1123848]: [INFO] ✅ NTP sync successful: offset=10.93ms, latency=15.25ms, server=time.cloudflare.com
Oct 29 09:42:58 portacode-streamer python3[1123848]: [INFO] 🔄 Starting periodic NTP sync...
Oct 29 09:42:58 portacode-streamer python3[1123848]: [INFO] ✅ NTP sync successful: offset=7.10ms, latency=5.28ms, server=time.cloudflare.com

menas@portacode-streamer:~/portacode$ 


not showing any errors, however, it got disconnected from the server and is still showing as offline on the server


# @ Update 16th of Decemner 2025

Very similar issue has been reported, with pretty much same symptoms, except that the device shows in the dashboard as online, but is irresponsive where it's not even updating CPU usage, not loading initial data when the client connects, no websocket messages at all from the device, yet it's reported as online in the dashboard. It's worth mentioning that when it happens, the only way we managed to restore the connection without killing the sessions running in the device (ie. without restarting the portacode service in the device) was by restarting the server itself, where it has to re-establish the connection. Hopefully restarting the internet connetion as well can fix the issue. That indicates that the issue is mostly that the websocket connection is actually alive but something in its driver is hanging and isn't even restarting