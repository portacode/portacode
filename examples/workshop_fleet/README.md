# Workshop Fleet Example

This example provisions ten identical Portacode containers so each student in a workshop receives a dedicated workspace plus a shared, read-only `instructions/` folder. Use it for in-person trainings or classrooms where the instructor wants to broadcast starter code and updates live.

## Layout

```
workshop_fleet/
├── Dockerfile             # builds python:3.11-slim + git + portacode
├── docker-compose.yaml    # defines 10 services (student-01 ... student-10)
├── initial_content/       # baked into the image and copied to each workspace
├── instructions/          # bind-mounted read-only at /root/workspace/instructions
└── data/student-XX/       # per-seat workspace + Portacode data directories
```

- `initial_content/` contains whatever starter project you want each student to get. The Dockerfile copies it into `/opt/initial_content`, and the compose command mirrors it into `/root/workspace` on container boot (using `cp -an` so existing changes are preserved).
- `instructions/` lives on the host. Modify the Markdown files during the workshop and every student immediately sees the changes inside their workspace at `/root/workspace/instructions/` without being able to edit them.
- `data/student-XX/workspace` is where each student actually works. The paired Portacode keys persist in `data/student-XX/.local/share/portacode/` so reconnects do not need fresh pairing codes.

## Usage

1. Request or generate a Portacode pairing code from the dashboard.
2. Export the code and start the stack:
   ```bash
   cd examples/workshop_fleet
   export PORTACODE_PAIRING_CODE=1234   # replace with your one-time code
   docker compose up -d                 # launches ten seats
   ```
3. Approve each pairing request in the dashboard. Every seat appears as “Workshop Seat 01”, “Workshop Seat 02”, etc., so you can identify them quickly.
4. Instruct students to open the Portacode web IDE for their assigned seat. Their `/root/workspace` already contains the files from `initial_content/`, and `/root/workspace/instructions` mirrors the host `instructions/` folder.

## Customizing

- Replace the contents of `initial_content/` with your starter project. The Dockerfile copies it into the image, and the runtime command seeds each workspace exactly once (`cp -an` prevents overwriting student edits on subsequent restarts).
- Edit `instructions/` during the session to broadcast announcements or additional tasks. Because the mount is read-only, students cannot accidentally change it.
- Increase or decrease the number of seats by duplicating/removing the service blocks in `docker-compose.yaml`. Just keep the naming consistent so you can identify devices from the dashboard.
