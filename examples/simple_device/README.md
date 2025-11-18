# Simple Device Example

This folder contains a minimal Docker Compose setup that runs the Portacode CLI in a single container. Use it to try out the pairing workflow or validate that bind mounts persist the device identity on the host.

## Prerequisites

- Docker + Docker Compose
- A Portacode account at [https://portacode.com](https://portacode.com)

## Usage

1. **Request a pairing code**  
   Log in to the dashboard and press **Pair Device**.  
   ![Pair Device button](https://raw.githubusercontent.com/portacode/portacode/master/docs/images/pair-device-button.png)

2. **Start the container**  
   ```bash
   cd examples/simple_device
   export PORTACODE_PAIRING_CODE=1234   # replace with your code
   docker compose up
   ```
   - The stack builds a tiny image (`python:3.11-slim + portacode`) and runs `portacode connect --non-interactive`.
   - Workspace files are mounted at `./data/device-01/workspace`.
   - Device keys persist in `./data/device-01/.local/share/portacode/keys/`.

3. **Approve the pairing request**  
   As soon as the container starts, the device appears in the dashboard with the name “Device 01” and project path `/root/workspace`.  
   ![Pairing request card](https://raw.githubusercontent.com/portacode/portacode/master/docs/images/pairing-request.png)
   Once approved, the RSA keypair remains in the mounted `.local/share/portacode` folder. Future `docker compose up` runs reconnect automatically without a new pairing code.


## Customizing

- Override `PORTACODE_DEVICE_NAME` before `docker compose up` to change the label.
- Edit `examples/simple_device/docker-compose.yaml` to add more `--project-path` flags or bind additional folders.
