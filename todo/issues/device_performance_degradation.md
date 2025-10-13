# Device Performance Degradation over time.

## **CONFIRMED ROOT CAUSE (2025-10-13)**

**Trigger: Opening/refreshing project workspace pages for git repositories**

Experimental findings:
- ✅ Terminal sessions have NO impact on performance (can run for days with heavy I/O)
- ✅ Project workspace pages WITHOUT .git folder: always fast, no degradation
- ❌ Project workspace pages WITH .git folder: **catastrophic degradation**
  - 1st load: takes several seconds
  - 2nd load: noticeably slower
  - 5th load: barely responsive, device goes offline momentarily

**Diagnostic evidence from running service (PID 1891992, 16+ hours uptime):**
- 200 pipes (normally <20)
- 65 threads (normally 5-10)
- 91% sustained CPU usage
- Performance: 55ms → 12,418ms (224x slowdown)

**Primary culprit: Watchdog file system watcher + Git operations**
- file_system_watcher.py:186-188 - `stop_watching()` doesn't unschedule watches
- file_system_watcher.py:168 - .git directories watched **recursively**
- manager.py:904 - Every project state refresh adds .git watch
- Result: Orphaned watches accumulate, every file change triggers 100+ unnecessary handlers

---

## Original Issue Description

When we run "portacode connect" or "portacode service install" and the device gets connected, initially, the totall time elapsed from the moment a client session sends a command to the device till the device response event arrivs back to the client session is typically less than 100ms with an average of 55ms. However, when we start actively using the device, the device starts to slow down gradually until within just a couple of hours or so, it becomes so slow it takes more than 12 seconds! The trace looks something like this:

{
    "client_send": 1760282723260.5,
    "ping": 12418,
    "server_receive": 1760282723264,
    "server_send": 1760282723278,
    "device_receive": 1760282727601,
    "handler_receive": 1760282728420,
    "handler_dispatch": 1760282728420,
    "handler_complete": 1760282735611,
    "device_send": 1760282735611,
    "server_receive_response": 1760282735611,
    "server_send_response": 1760282735611,
    "client_receive": 1760282735678.5
}

 Client → Server: 3.50ms
 Server Processing: 14.00ms
 Server → Device: 4323.00ms
 Device Processing: 819.00ms
 Handler Queue: 0.00ms
 Handler Execution: 7191.00ms
 Device Response: 0.00ms
 Device → Server: 0.00ms
 Server → Client: 67.50ms
TOTAL: 12418.00ms

While the trace might missleadingly make it look like the connection Server → Device is contributing to the issue, but the truth is that the timestamp taken in the device side not immidiately as soon as the message is actually received. It's also clear from the "Device Processing" and the "Handler Execution" that the device is actually very slow. We also tried closing all terminal sessions in the device assuming some might be overwhelming it with the size of their buffer or so but that didn't change anything and it stayed very slow until we restart the portacode service.


  Hypothesis 1: Asyncio Task Accumulation (Memory Leak)

  Location: Multiple locations throughout the codebase

  Evidence:
  - asyncio.create_task() is called extensively but tasks are rarely tracked or cleaned up:
    - Terminal session debounce tasks: /home/menas/portacode/portacode/connection/handlers/session.py:260
    - File watcher event handling: /home/menas/portacode/portacode/connection/handlers/project_state/file_system_watcher.py:125-128
    - Project state refresh tasks: /home/menas/portacode/portacode/connection/terminal.py:502-503
    - Git change callbacks: /home/menas/portacode/portacode/connection/handlers/project_state/manager.py:840

  Why it causes degradation:
  - Each uncancelled/uncompleted task consumes memory and CPU
  - Over hours of usage, thousands of orphaned tasks accumulate in the event loop
  - The event loop has to check all pending tasks on each iteration, causing exponential slowdown
  - The 7191ms "Handler Execution" time suggests the event loop is overwhelmed

  Key smoking gun: Line 260 in session.py creates debounce tasks for terminal data that may never complete if terminals are long-running. Similarly, line 840 in
  manager.py creates tasks without storing references to cancel them later.

  ---
  Hypothesis 2: Git Operations Blocking the Event Loop

  Location: /home/menas/portacode/portacode/connection/handlers/project_state/git_manager.py and
  /home/menas/portacode/portacode/connection/handlers/project_state/manager.py

  Evidence:
  - Git periodic monitoring runs every 1 second checking full repository status: git_manager.py:1839
  - File system watcher triggers git operations on every file change: manager.py:842-878
  - Many git operations are NOT using run_in_executor despite being synchronous I/O
  - get_file_status_batch() performs multiple git commands per file: git_manager.py:295-406

  Why it causes degradation:
  - Git operations on large repos can take 100-500ms each
  - With active file editing, git operations are triggered continuously
  - The synchronous git calls block the event loop, preventing handlers from processing
  - The 819ms "Device Processing" + 7191ms "Handler Execution" = 8010ms spent in device-side operations

  Key smoking gun: Line 1839 shows monitoring runs every 1 second, and lines 372-407 show batch git operations that aren't async-wrapped, causing cumulative
  blocking over time.

  ---
  Hypothesis 3: Watchdog File System Watcher Resource Leak

  Location: /home/menas/portacode/portacode/connection/handlers/project_state/file_system_watcher.py and
  /home/menas/portacode/portacode/connection/handlers/project_state/manager.py

  Evidence:
  - Watchdog observers are created but never stopped: file_system_watcher.py:136
  - .git directories are watched recursively: file_system_watcher.py:168
  - New paths are added to watched_paths but cleanup only discards from set without stopping observers: file_system_watcher.py:180-188
  - Multiple project sessions can create multiple watchers for overlapping paths

  Why it causes degradation:
  - Each watchdog observer creates background threads that consume resources
  - Recursive watching of .git/objects/ can trigger thousands of events during git operations
  - File system events queue up faster than they can be processed
  - The cross-thread communication (watchdog thread → asyncio event loop) adds overhead

  Key smoking gun: Line 168 watches git directories recursively, and line 186 shows stop_watching() only removes from the set but doesn't actually stop the observer
   schedules. The Observer instance lives for the lifetime of the watcher, accumulating schedules.

  ---
  Recommended Investigation Order:

  1. Check for task accumulation first (use asyncio.all_tasks() to count pending tasks)
  2. Profile git operation frequency and duration
  3. Check watchdog observer thread count and file descriptor usage