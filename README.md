# Harbor Airgapped Automated Deployment

Automated end-to-end deployment of [Harbor](https://goharbor.io/) container registry in a fully airgapped environment on **VMware Photon OS**.

---

## Overview

This project provides two scripts:

| Script | Purpose | Run on |
|---|---|---|
| `download_prerequisites.sh` | Downloads all required packages and binaries | Internet-connected machine |
| `deploy_harbor.py` | Deploys and configures Harbor end-to-end | Airgapped Photon OS host |

The deployment script handles everything from OS-level configuration to a running, TLS-secured Harbor instance with a dedicated data disk.

---

## Requirements

### Download machine (internet-connected)
- `curl`
- `md5sum`

### Target host (airgapped)
- VMware Photon OS 5.0 (or 4.0)
- Python 3 with `PyYAML` (`pip install pyyaml`)
- A secondary disk (unformatted) for Harbor data — the script will partition, format, and mount it
- TLS certificate, key, and CA cert pre-generated and placed at the paths defined in `config.yaml`

---

## Repository Structure

```
.
├── download_prerequisites.sh   # Downloads all packages to /opt/packages
├── deploy_harbor.py            # End-to-end Harbor deployment
└── config.yaml                 # All deployment inputs (edit before deploying)
```

---

## Quickstart

### Step 1 — Download prerequisites (internet-connected machine)

```bash
chmod +x download_prerequisites.sh
./download_prerequisites.sh
```

By default, the script auto-detects the latest versions of Harbor, docker-compose, and all Photon OS RPMs. To pin a specific version, set it at the top of the script:

```bash
HARBOR_VERSION="v2.10.2"       # pin Harbor version
DOCKER_COMPOSE_VERSION=""       # blank = auto-detect latest
```

All files are saved to `/opt/packages`. Transfer the directory to the airgapped host:

```bash
scp -r /opt/packages user@airgapped-host:/opt/
```

### Step 2 — Edit `config.yaml`

Update the configuration file with your environment details before deploying. At minimum, set:

- `harbor.hostname` — FQDN for the registry
- `harbor.admin_password` and `database.password`
- `harbor.data_volume` — mount point for the secondary data disk
- `installer.offline_package` — path to the downloaded `.tgz`
- `certificates.*` — paths to your CA cert, server cert, and server key
- `packages.*` — paths to the downloaded RPMs and docker-compose binary

### Step 3 — Deploy (airgapped host)

```bash
sudo python3 deploy_harbor.py config.yaml
```

---

## What the Deployment Script Does

The script runs the following steps in order:

1. **Disk setup** — Detects the secondary disk, partitions and formats it as ext4, mounts it at `data_volume`, and persists the mount in `/etc/fstab`. Errors out if no secondary disk is found.
2. **OS configuration** — Loads `br_netfilter`, enables IP forwarding, and persists sysctl settings.
3. **Docker** — Installs Docker from local RPMs via `tdnf` and enables the service.
4. **docker-compose** — Installs the standalone binary to `/usr/local/bin`.
5. **TLS certificates** — Installs the CA into Docker's trust store and the system CA store.
6. **Harbor extraction** — Unpacks the offline installer tarball.
7. **Harbor configuration** — Generates `harbor.yml` from the bundled template.
8. **Harbor installation** — Runs `install.sh` with any enabled optional components.
9. **systemd service** — Registers Harbor as a systemd service for auto-start on boot.
10. **Health check** — Pings the Harbor API to confirm the deployment is live.

---

## Configuration Reference

```yaml
harbor:
  hostname: registry.example.com   # FQDN — must resolve on the host
  http_port: 80
  https_port: 443
  admin_password: "Harbor12345!"
  data_volume: /data/harbor         # Mount point for the secondary data disk

installer:
  offline_package: /opt/packages/harbor-offline-installer-v2.10.2.tgz
  install_dir: /opt

certificates:
  ca_cert: /opt/certs/ca.crt
  server_cert: /opt/certs/server.crt
  server_key: /opt/certs/server.key

database:
  password: "DBpassword123"

packages:
  docker_rpms:
    - /opt/packages/docker-25.0.6-1.ph5.x86_64.rpm
    - /opt/packages/containerd-1.7.13-1.ph5.x86_64.rpm
    - /opt/packages/docker-cli-25.0.6-1.ph5.x86_64.rpm
    - /opt/packages/libseccomp-2.5.3-2.ph5.x86_64.rpm
    - /opt/packages/parted-3.5-2.ph5.x86_64.rpm
  docker_compose_binary: /opt/packages/docker-compose-linux-x86_64

components:
  trivy: false        # Vulnerability scanner
  notary: false       # Content trust / image signing
  chartmuseum: false  # Helm chart repository

systemd:
  enable_service: true
```

---

## Optional Components

Set any of the following to `true` in `config.yaml` to include them in the Harbor installation:

| Component | Key | Description |
|---|---|---|
| Trivy | `components.trivy` | Image vulnerability scanning |
| Notary | `components.notary` | Content trust and image signing |
| ChartMuseum | `components.chartmuseum` | Helm chart repository |

---

## Notes

- The deployment script is **idempotent** for most steps — re-running it will skip already-completed steps (Docker already installed, disk already mounted, certs already in place, etc.).
- RPM package names in `config.yaml` must match the exact filenames downloaded by `download_prerequisites.sh`. If the auto-resolved versions differ from the defaults shown, update the paths accordingly.
- The `download_prerequisites.sh` script will re-use files already present in `OUTPUT_DIR` and skip re-downloading them.
