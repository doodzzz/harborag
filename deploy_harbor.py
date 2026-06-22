#!/usr/bin/env python3
"""
Harbor airgapped deployment script for Photon OS.
Usage: python3 deploy_harbor.py [config.yaml]
"""

import os
import sys
import ssl
import shutil
import tarfile
import logging
import subprocess
import urllib.request
import yaml
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def run(cmd: str, check: bool = True) -> subprocess.CompletedProcess:
    log.info(f"$ {cmd}")
    result = subprocess.run(cmd, shell=True, text=True, capture_output=True)
    if result.stdout.strip():
        log.debug(result.stdout.strip())
    if result.returncode != 0:
        if check:
            log.error(result.stderr.strip())
            raise RuntimeError(f"Command failed: {cmd}")
        log.warning(result.stderr.strip())
    return result


def check_root():
    if os.geteuid() != 0:
        log.error("Script must run as root.")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Disk setup
# ---------------------------------------------------------------------------

def find_secondary_disk() -> str:
    """Return the device path of the first disk that is not the OS root disk."""
    root_dev = run("findmnt -n -o SOURCE /").stdout.strip()
    os_disk  = run(f"lsblk -ndo PKNAME {root_dev}").stdout.strip()

    for line in run("lsblk -ndo NAME,TYPE").stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == "disk" and parts[0] != os_disk:
            return f"/dev/{parts[0]}"
    return ""


def format_and_mount_data_disk(cfg: dict):
    """
    Locate the secondary disk, format it as ext4, mount it at data_volume,
    and persist the entry in /etc/fstab.
    - Skips the format/mount if data_volume is already a mounted filesystem.
    - Skips the format if the partition already carries a filesystem.
    - Errors out if no secondary disk is attached.
    """
    mount_point = cfg["harbor"]["data_volume"]

    if run(f"mountpoint -q {mount_point}", check=False).returncode == 0:
        log.info(f"{mount_point} is already mounted — skipping disk setup.")
        return

    disk = find_secondary_disk()
    if not disk:
        raise RuntimeError(
            "No secondary disk detected. Attach a data disk before deploying Harbor."
        )
    log.info(f"Secondary disk: {disk}")

    partition = f"{disk}1"

    # Only partition and format if no filesystem exists on the target partition
    if run(f"blkid {partition}", check=False).returncode != 0:
        run(f"parted -s {disk} mklabel gpt mkpart primary ext4 0% 100%")
        run("partprobe", check=False)
        run(f"mkfs.ext4 -F {partition}")
        log.info(f"Formatted {partition} as ext4.")
    else:
        log.info(f"{partition} already has a filesystem — skipping format.")

    Path(mount_point).mkdir(parents=True, exist_ok=True)
    run(f"mount {partition} {mount_point}")

    uuid = run(f"blkid -s UUID -o value {partition}").stdout.strip()
    fstab_entry = f"UUID={uuid}  {mount_point}  ext4  defaults  0  2\n"
    fstab = Path("/etc/fstab").read_text()
    if uuid not in fstab:
        with open("/etc/fstab", "a") as f:
            f.write(fstab_entry)

    log.info(f"Data disk mounted at {mount_point} (UUID={uuid}).")


# ---------------------------------------------------------------------------
# OS-level configuration
# ---------------------------------------------------------------------------

def configure_os():
    """Enable IP forwarding, load br_netfilter, and persist sysctl settings."""
    sysctl_params = {
        "net.ipv4.ip_forward": "1",
        "net.bridge.bridge-nf-call-iptables": "1",
        "net.bridge.bridge-nf-call-ip6tables": "1",
    }

    run("modprobe br_netfilter", check=False)
    Path("/etc/modules-load.d/harbor.conf").write_text("br_netfilter\n")

    conf = "\n".join(f"{k} = {v}" for k, v in sysctl_params.items())
    Path("/etc/sysctl.d/99-harbor.conf").write_text(conf + "\n")
    run("sysctl --system")

    log.info("OS configuration applied.")


# ---------------------------------------------------------------------------
# Prerequisites
# ---------------------------------------------------------------------------

def install_docker(cfg: dict):
    """Install Docker from local RPMs using tdnf (Photon OS package manager)."""
    if run("docker --version", check=False).returncode == 0:
        log.info("Docker already installed — skipping.")
        return

    rpms = " ".join(cfg["packages"]["docker_rpms"])
    run(f"tdnf install -y {rpms}")
    run("systemctl enable --now docker")
    log.info("Docker installed and started.")


def install_docker_compose(cfg: dict):
    """Install docker-compose binary from local path."""
    dest = Path("/usr/local/bin/docker-compose")
    if dest.exists():
        log.info("docker-compose already present — skipping.")
        return

    shutil.copy2(cfg["packages"]["docker_compose_binary"], dest)
    dest.chmod(0o755)
    log.info(f"docker-compose installed to {dest}.")


def prepare_certificates(cfg: dict):
    """
    Install TLS certificates:
      - Docker trust store  (/etc/docker/certs.d/<hostname>/)
      - System CA store     (/etc/ssl/certs/)
    """
    hostname = cfg["harbor"]["hostname"]
    certs = cfg["certificates"]

    # Docker cert trust
    docker_dir = Path(f"/etc/docker/certs.d/{hostname}")
    docker_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(certs["ca_cert"], docker_dir / "ca.crt")
    log.info(f"CA certificate installed to Docker trust store: {docker_dir}")

    # System-wide CA trust
    ca_system = Path("/etc/ssl/certs") / Path(certs["ca_cert"]).name
    shutil.copy2(certs["ca_cert"], ca_system)
    run("update-ca-certificates", check=False)

    run("systemctl restart docker")
    log.info("Certificates installed and Docker restarted.")


# ---------------------------------------------------------------------------
# Harbor installation
# ---------------------------------------------------------------------------

def extract_installer(cfg: dict) -> Path:
    """Extract the Harbor offline tarball and return the harbor directory path."""
    pkg = cfg["installer"]["offline_package"]
    install_dir = Path(cfg["installer"]["install_dir"])
    install_dir.mkdir(parents=True, exist_ok=True)

    log.info(f"Extracting {pkg} → {install_dir}")
    with tarfile.open(pkg, "r:gz") as tar:
        tar.extractall(install_dir)

    return install_dir / "harbor"


def generate_harbor_config(cfg: dict, harbor_dir: Path):
    """
    Build harbor.yml from the bundled template.
    Certs are copied into <harbor_dir>/certs/ so paths survive relocation.
    """
    template = harbor_dir / "harbor.yml.tmpl"
    output = harbor_dir / "harbor.yml"
    h = cfg["harbor"]
    certs = cfg["certificates"]

    # Stage certs inside the harbor directory
    cert_dir = harbor_dir / "certs"
    cert_dir.mkdir(exist_ok=True)
    shutil.copy2(certs["server_cert"], cert_dir / "server.crt")
    shutil.copy2(certs["server_key"], cert_dir / "server.key")

    harbor_cfg = yaml.safe_load(template.read_text())
    harbor_cfg["hostname"] = h["hostname"]
    harbor_cfg["http"]["port"] = h.get("http_port", 80)
    harbor_cfg["https"]["port"] = h.get("https_port", 443)
    harbor_cfg["https"]["certificate"] = str(cert_dir / "server.crt")
    harbor_cfg["https"]["private_key"] = str(cert_dir / "server.key")
    harbor_cfg["harbor_admin_password"] = h["admin_password"]
    harbor_cfg["database"]["password"] = cfg["database"]["password"]
    harbor_cfg["data_volume"] = h["data_volume"]

    with open(output, "w") as f:
        yaml.dump(harbor_cfg, f, default_flow_style=False, allow_unicode=True)

    log.info(f"harbor.yml written to {output}.")


def run_installer(cfg: dict, harbor_dir: Path):
    """Execute Harbor's install.sh with optional component flags."""
    components = cfg.get("components", {})
    flag_map = [
        ("trivy", "--with-trivy"),
        ("notary", "--with-notary"),
        ("chartmuseum", "--with-chartmuseum"),
    ]
    flags = [flag for key, flag in flag_map if components.get(key)]
    run(f"bash {harbor_dir}/install.sh {' '.join(flags)}")


# ---------------------------------------------------------------------------
# Post-install
# ---------------------------------------------------------------------------

def configure_systemd(cfg: dict, harbor_dir: Path):
    """Register Harbor as a systemd service so it starts on boot."""
    if not cfg.get("systemd", {}).get("enable_service", True):
        return

    service = f"""[Unit]
Description=Harbor Container Registry
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory={harbor_dir}
ExecStart=/usr/local/bin/docker-compose up -d
ExecStop=/usr/local/bin/docker-compose down
TimeoutStartSec=0

[Install]
WantedBy=multi-user.target
"""
    Path("/etc/systemd/system/harbor.service").write_text(service)
    run("systemctl daemon-reload")
    run("systemctl enable harbor")
    log.info("Harbor systemd service registered and enabled.")


def verify_deployment(cfg: dict):
    """Ping the Harbor API to confirm the service is up."""
    hostname = cfg["harbor"]["hostname"]
    port = cfg["harbor"].get("https_port", 443)
    url = f"https://{hostname}:{port}/api/v2.0/ping"

    ctx = ssl.create_default_context()
    ctx.load_verify_locations(cfg["certificates"]["ca_cert"])

    try:
        with urllib.request.urlopen(url, context=ctx, timeout=20) as r:
            log.info(f"Harbor health check OK — response: {r.read().decode().strip()}")
    except Exception as e:
        log.warning(f"Health check failed ({e}). Harbor may still be initializing.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    cfg = load_config(cfg_path)

    check_root()
    format_and_mount_data_disk(cfg)
    configure_os()
    install_docker(cfg)
    install_docker_compose(cfg)
    prepare_certificates(cfg)
    harbor_dir = extract_installer(cfg)
    generate_harbor_config(cfg, harbor_dir)
    run_installer(cfg, harbor_dir)
    configure_systemd(cfg, harbor_dir)
    verify_deployment(cfg)

    log.info("Harbor deployment complete.")


if __name__ == "__main__":
    main()
