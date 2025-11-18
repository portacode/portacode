# Workshop Fleet Example

This folder contains a “10-seat lab” version of Portacode that lets a teacher spin up identical workspaces for students plus a shared read-only instructions board.

## Who This Is For

- Teachers or trainers who want to hand each learner a ready-to-code workspace (no local installs required).
- Facilitators who need a central place to post instructions or updates that instantly appear for every student.
- Anyone comfortable running a few terminal commands but new to Docker/containers.

## Before You Begin

Make sure these free tools are installed on the computer that will host the workshop:

| Tool | What it does | Install |
| --- | --- | --- |
| Git | Downloads the Portacode repository and keeps it updated. | [git-scm.com/downloads](https://git-scm.com/downloads) |
| Docker | Runs lightweight “containers” (think: pre-configured mini-computers). | [docs.docker.com/get-docker](https://docs.docker.com/get-docker/) |
| Docker Compose | Lets Docker start several containers at once using one file. Comes with Docker Desktop; Linux install steps are [here](https://docs.docker.com/compose/install/). |

Verify each tool with:

```bash
git --version
docker --version
docker compose version
```

> 📝 **Tip:** Docker Desktop already bundles Docker Compose on macOS and Windows. On Linux, install Docker first, then follow the Compose link above.

## Get the Workshop Files

1. Open a terminal and clone the repository:
   ```bash
   git clone https://github.com/portacode/portacode.git
   ```
2. Move into the Workshop Fleet folder:
   ```bash
   cd portacode/examples/workshop_fleet
   ```

You now have a ready-made Dockerfile, docker-compose stack, starter content, and sample instructions.

## Folder Tour

```
workshop_fleet/
├── Dockerfile             # builds python:3.11-slim + git + portacode CLI
├── docker-compose.yaml    # defines 10 seats (student-01 ... student-10)
├── initial_content/       # files each student gets on first boot
├── instructions/          # shared, read-only notes shown to every student
└── data/student-XX/       # per-seat workspaces + Portacode keys (auto-created)
```

- `initial_content/` is copied into each student’s `/root/workspace` the first time their container starts. Replace it with your starter project, lab files, etc.
- `instructions/` is bind-mounted read-only to `/root/workspace/instructions`. Update the Markdown in this folder during the workshop to broadcast announcements instantly—students can read it but cannot edit it.
- `data/student-XX/` stores each learner’s editable workspace plus their Portacode keys so reconnections do not require new pairing codes. The folders are created automatically the first time you run the stack.

## Request a Portacode Pairing Code

You can get one pairing code from the dashboard at [https://portacode.com](https://portacode.com) and use it to pair all containers in one step, and later you can tranfer each one to a student.

1. Sign in to the dashboard and click **Pair Device**:  
   ![Pair Device button](https://raw.githubusercontent.com/portacode/portacode/master/docs/images/pair-device-button.png)
2. A four-digit code appears (valid for 15 minutes). This code only authorizes the request—you still approve each seat/device/container on the dashboard after the container connects.

Keep the code handy; you will export it before starting Docker Compose.

## Start the Workshop Fleet

1. Inside `examples/workshop_fleet`, set the `PORTACODE_PAIRING_CODE` for **your operating system**:
   - macOS/Linux (Bash, Zsh):
     ```bash
     export PORTACODE_PAIRING_CODE=1234
     ```
   - Windows PowerShell:
     ```powershell
     $env:PORTACODE_PAIRING_CODE = "1234"
     ```
   - Windows Command Prompt:
     ```cmd
     set PORTACODE_PAIRING_CODE=1234
     ```
   Replace `1234` with the active pairing code from the dashboard.
2. Launch the full classroom (10 seats) in the background:
   ```bash
   docker compose up -d
   ```
3. Check that all seats are running:
   ```bash
   docker compose ps
   ```

Each service is named `student-01`, `student-02`, … `student-10`. During the first run Docker builds the custom image, seeds `/root/workspace` with your `initial_content/`, and registers `/root/workspace` as the initial Portacode project path for every device (identical to the `examples/simple_device` behavior). If you need additional project paths, set `PORTACODE_PROJECT_PATHS=/root/workspace:/root/workspace/instructions` (or any colon-separated list) before `docker compose up`; the bundled helper script converts that list into repeated `--project-path` flags automatically.

## Approve the Devices (Seats)

Portacode refers to each container as a **device**, even though you may call them seats in class. Approving them keeps ownership under the instructor account until you transfer control.

1. Within a few seconds of running `docker compose up`, every device appears on your dashboard as “Workshop Seat 01”, “Workshop Seat 02”, etc.  
   ![Pairing request card](https://raw.githubusercontent.com/portacode/portacode/master/docs/images/pairing-request.png)
2. Click **Approve** on each card. The device remains on your dashboard (under your account) until you hand it off to a student.

## Transfer Each Device to a Student

After approval, instruct students to sign in at [https://portacode.com](https://portacode.com) and follow these steps so they own their seat:

1. On the dashboard, hover or tap the device and select the **Transfer Ownership** icon.  
   ![Device transfer button highlighted on the Portacode dashboard](https://raw.githubusercontent.com/portacode/portacode/master/docs/images/device-transfer-button.png)
2. Enter the student’s email address in the modal and click **Send invite**.  
   ![Device transfer modal showing recipient email field](https://raw.githubusercontent.com/portacode/portacode/master/docs/images/device-transfer-modal.png)
3. Once the student accepts the invitation, the device moves to their account. You still retain filesystem access to `examples/workshop_fleet/data/student-XX/` on the host, which contains their workspace and Portacode data for grading or troubleshooting.

> 🔍 **Need to review submissions?** Explore `data/student-XX/workspace/` directly from the host. Every edit students make inside Portacode is persisted there.

## What Students See

- `/root/workspace` contains their editable copy of `initial_content/`. The helper script only copies files that do not already exist, so restarts never overwrite progress.
- `/root/workspace/instructions` mirrors the host `instructions/` folder. You can update Markdown live, but the mount is read-only for students.
![Stodent Portacode IDE Desktop View](https://raw.githubusercontent.com/portacode/portacode/master/docs/images/student-workspace.png)
- Portacode keys live in `/root/.local/share/portacode`, mounted under `data/student-XX/` on the host. Restarting or rebuilding containers reuses these keys, so no new pairing codes are needed unless you delete the folder.

## Customize for Your Class

- **Starter project:** Replace `initial_content/` and rebuild (`docker compose build`). The baked image plus the seeding script ensure every device starts with the same files.
- **Live instructions:** Edit Markdown in `instructions/` (for example, `instructions/WELCOME.md`) to broadcast updates instantly.
- **Seat count and names:** Edit `docker-compose.yaml`. Duplicating/removing the `student-XX` blocks changes how many devices you run; `PORTACODE_DEVICE_NAME` defines the label shown on the dashboard.
- **Reset a seat:** `docker compose stop student-03`, delete `data/student-03/`, and start it again. The workspace is reseeded and the device will prompt for approval/transfer again.
- **Register more paths:** Set `PORTACODE_PROJECT_PATHS` before `docker compose up -d` to register additional folders automatically.
- **Bring your own repo:** Point `initial_content/` to your materials or extend the Dockerfile to clone a repository while the image builds.

## Clean Up or Pause

- Pause the classroom but keep data: `docker compose stop`
- Shut everything down and keep data for later: `docker compose down`
- Remove containers **and** delete student work: `docker compose down --volumes` (only if you are done with the workshop)

## Troubleshooting

- **Docker command not found:** Re-run the Docker installation for your OS and be sure to reboot/log out as requested in the Docker docs.
- **Ports already in use:** The compose file maps each Portacode seat to a unique port. If another app is using one of them, edit `docker-compose.yaml`, then run `docker compose up -d`.
- **Device stuck in “Pending approval”:** Verify the correct `PORTACODE_PAIRING_CODE` is set for your shell. If the code expired, stop the stack, request a fresh code, set it again (see the platform-specific commands above), and rerun `docker compose up -d`.
- **Need logs:** `docker compose logs student-04` shows what is happening in a specific container.
- **Update to a newer Portacode version:** `git pull` to update this repository, then `docker compose build --pull` before `docker compose up -d`.

## Next Steps

- Add announcement files under `instructions/` (for example, `instructions/SCHEDULE.md`).
- Swap the sample `initial_content/` notebook/app for your actual assignment.
- Use the pairing + transfer workflow whenever you add more seats or rebuild.

With these pieces in place, you can provision devices, approve them, and transfer control to students with only a few commands.
