# Portacode Examples

This directory hosts ready-to-run Docker Compose demos that also ship with the PyPI source distribution. Copy any of them to bootstrap your own deployment.

- `simple_device/` – a single-container sandbox that showcases how Portacode can turn even the tiniest Linux host into a remotely accessible development box. Point it at a bind-mounted project, approve the device once, and you instantly have a persistent, browser-based terminal wherever that container runs—perfect for lab servers, NAS devices, or any environment where SSH access is inconvenient.

- `workshop_fleet/` – a classroom-ready stack aimed at instructors who want to hand every attendee their own cloud-accessible dev box. It provisions ten identical containers, seeds each workspace with the same starter project via `initial_content/`, and mounts a shared read-only `instructions/` folder so the instructor can broadcast live updates without students overwriting them. This model works well for coding bootcamps, hackathons, or support rotations where you need controlled, reproducible workspaces.

Each subfolder contains its own README with detailed setup steps and customization ideas.
