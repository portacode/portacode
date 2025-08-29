# Issue: Terminal session exits immediately after start (intermittent; device-sticky until service restart)

## What’s happening (facts)

* **Symptom:** Adevice would be working fine until suddenly it gets locked in a state where starting any new terminal session results in the terminal exiting immediately after.
* **Intermittency:** The issue **doesn’t always happen**; usually things work.
* **Device stickiness:** When a **device** experiences the issue, **every attempt on that device** behaves the same (immediate exit) **even if starting in a different project folder or even in the dashboard**.
* **Persistence & workaround:** The problem **persists until the Portacode service on the device is restarted**; restarting clears it (until it recurs).

## Command used

* To view logs: `portacode service status -v`

## Log evidence (exact lines, in order)

```
Aug 28 08:52:17 portacode-streamer python3[109754]: [INFO] registry: Dispatching command 'terminal_start' with reply_channel=None
Aug 28 08:52:17 portacode-streamer python3[109754]: [INFO] handler: Processing command terminal_start with reply_channel=None
Aug 28 08:52:17 portacode-streamer python3[109754]: [INFO] Launching terminal 34ac592951c84a7b823ccd0432527d75 using shell=bash on channel=34ac592951c84a7b823ccd0432527d75
Aug 28 08:52:17 portacode-streamer python3[109754]: [INFO] handler: Command terminal_start executed successfully
Aug 28 08:52:17 portacode-streamer python3[109754]: [INFO] handler: terminal_start response project_id=1556b16d-a4a7-440e-b872-5ba82d756848, response={'event': 'terminal_started', 'terminal_id': '34ac592951c84a7b823ccd0432527d75', 'channel': '34ac592951c84a7b823ccd0432527d75', 'pid': 4001561, 'shell': 'bash', 'cwd': '/home/menas/portacode', 'project_id': '1556b16d-a4a7-440e-b872-5ba82d756848'}
Aug 28 08:52:17 portacode-streamer python3[109754]: [INFO] registry: Successfully dispatched command 'terminal_start'
Aug 28 08:52:20 portacode-streamer python3[109754]: [INFO] session_manager: Removed session 34ac592951c84a7b823ccd0432527d75 (PID: 4001561) from session manager
Aug 28 08:52:21 portacode-streamer python3[109754]: [ERROR] Task was destroyed but it is pending!
Aug 28 08:52:21 portacode-streamer python3[109754]: task: <Task pending name='Task-23986288' coro=<TerminalSession.start_io_forwarding.<locals>._pump() running at /home/menas/.local/lib/python3.11/site-packages/portacode/connection/handlers/session.py:70> wait_for=<Future pending cb=[Task.task_wakeup()]>> 
Aug 28 08:52:22 portacode-streamer python3[109754]: [ERROR] [DEBUG] Error loading directory /home/menas/portacode: [Errno 9] Bad file descriptor: '/home/menas/portacode'
Aug 28 08:52:55 portacode-streamer python3[109754]: [INFO] terminal_manager: Processing command 'client_sessions_update' with reply_channel=None
Aug 28 08:52:55 portacode-streamer python3[109754]: [INFO] terminal_manager: 🔔 RECEIVED client_sessions_update with 1 sessions
Aug 28 08:52:55 portacode-streamer python3[109754]: [INFO] Updated client sessions: 1 sessions, 0 newly added, 1 disconnected
Aug 28 08:52:55 portacode-streamer python3[109754]: [INFO] Disconnected sessions: ['specific..inmemory!VfoFkZPqGxjL']
Aug 28 08:52:55 portacode-streamer python3[109754]: [INFO] Project states preserved for potential reconnection of these sessions
Aug 28 08:52:55 portacode-streamer python3[109754]: [INFO] terminal_manager: ✅ Updated client sessions (1 sessions)
Aug 28 08:52:55 portacode-streamer python3[109754]: [INFO] terminal_manager: ℹ️ No new sessions to send data to
Aug 28 08:52:55 portacode-streamer python3[109754]: [INFO] [DEBUG] get_or_create_project_state_manager called
Aug 28 08:52:55 portacode-streamer python3[109754]: [INFO] [DEBUG] Context debug flag: False
Aug 28 08:52:55 portacode-streamer python3[109754]: [INFO] [DEBUG] Returning existing GLOBAL project state manager (PID: 109754)
```

## Concrete facts derivable from the logs

* **Terminal launch reported as successful** at `08:52:17` with:

  * `terminal_id`: `34ac592951c84a7b823ccd0432527d75`
  * `channel`: `34ac592951c84a7b823ccd0432527d75`
  * `pid`: `4001561`
  * `shell`: `bash`
  * `cwd`: `/home/menas/portacode`
  * `project_id`: `1556b16d-a4a7-440e-b872-5ba82d756848`
* **Session removal** occurred **3 seconds later** at `08:52:20` for the same terminal ID and PID.
* After removal, logs show:

  * An **async task** message: “Task was destroyed but it is pending!” referencing `TerminalSession.start_io_forwarding.<locals>._pump()` at `session.py:70`.
  * A **directory error**: “Error loading directory /home/menas/portacode: \[Errno 9] Bad file descriptor”.
* A subsequent **client sessions update** indicates **1 disconnected session** and that **project states were preserved**.
* The system indicates it is **using an existing GLOBAL project state manager** (process PID shown as `109754`).

## What is **not** present in the provided data

* No **exit code** or **signal** for PID `4001561` is printed.
* No explicit **permission denied** messages.
* No explicit **resource limit** or **out-of-file-descriptor** message.
* No timezone information in the timestamps (only date and time as logged).

---

If you want me to go further, I can keep it factual and transform this into a minimal repro checklist or collect-only diagnostics (no hypotheses)—but I’ll stop here unless you say otherwise.
