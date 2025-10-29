# Portacode Project Connection CPU Hotspots

## Context
Devices spike to 100% CPU as soon as a client session connects to specific projects. The analysis below highlights the most expensive paths invoked during project onboarding and state sync, pinpointing the five most likely culprits.

## Top 5 Likely Causes
1. **Full project rescan on every refresh**  
   - `portacode/connection/handlers/project_state/manager.py:361` rebuilds the entire project item list on each refresh, walking every monitored directory and re-sorting the full item set.  
   - During the same pass, `git_status_map` is populated via a threadpool call into `GitManager.get_file_status_batch` (`portacode/connection/handlers/project_state/manager.py:396` → `portacode/connection/handlers/project_state/git_manager.py:295`), which shells out to several `git` commands (`status`, `diff`, `ls-files`). For large trees this keeps CPU saturated.

2. **Recursive `.git` watcher flood**  
   - Every project session starts a recursive watchdog on the `.git` directory (`portacode/connection/handlers/project_state/file_system_watcher.py:170`).  
   - The handler forwards virtually any qualifying Git event into `_handle_file_change` (`portacode/connection/handlers/project_state/manager.py:816`), which in turn calls `_refresh_project_state` (`portacode/connection/handlers/project_state/manager.py:881`).  
   - Busy repositories generate continuous `.git/objects` churn, so the device keeps re-running the heavy refresh path above even when workspace files stay untouched.

3. **Unbounded project-state initialisers**  
   - Each `client_sessions_update` spawns `_manage_project_states_for_session_changes` via `loop.create_task` without guard rails (`portacode/connection/terminal.py:96-104`).  
   - Rapid consecutive updates therefore overlap the expensive `ProjectStateManager.initialize_project_state` calls inside that coroutine (`portacode/connection/terminal.py:192` / `portacode/connection/handlers/project_state/manager.py:131`). Multiple full scans and git probes run simultaneously, pegging CPU.

4. **Per-session git monitoring loops**  
   - A fresh `GitManager` is created for every client session, even when they point at the same project folder (`portacode/connection/handlers/project_state/manager.py:146-155`).  
   - Each instance schedules its own `_monitor_git_changes` loop (`portacode/connection/handlers/project_state/git_manager.py:1803` → `portacode/connection/handlers/project_state/git_manager.py:1843`) that executes `git status`, `git diff` and related commands every five seconds. With multiple viewers attached to a large repo, these redundant polls easily saturate CPU.

5. **Hashing every changed file on each update**  
   - `_refresh_project_state` refreshes `git_detailed_status` for every run (`portacode/connection/handlers/project_state/manager.py:905-912`).  
   - `GitManager.get_detailed_status` (`portacode/connection/handlers/project_state/git_manager.py:1187-1292`) computes SHA hashes for each staged, unstaged and untracked file by reading their full contents. When sizeable files change—or when the refresh path is invoked frequently via the `.git` watcher above—this repeated hashing drives sustained CPU usage.

## Recommended Next Steps
- Introduce incremental diffing (e.g., track dirty directories) so `_build_flattened_items_structure` only touches changed paths.
- Throttle or narrow `.git` monitoring (consider ignoring `objects/` and batching events).
- Debounce `client_sessions_update` orchestration so only one state-initialisation task can run per session/project at a time.
- Promote `GitManager` to be shared per project/root and pause the periodic monitor when no deltas are detected.
- Cache git detailed status / file hashes and invalidate selectively instead of recomputing every refresh.
