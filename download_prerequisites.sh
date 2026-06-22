#!/usr/bin/env bash
# download_prerequisites.sh
#
# Downloads all Harbor airgapped deployment prerequisites.
# Versions are resolved automatically from upstream unless pinned below.
# Run on a machine WITH internet access, then transfer OUTPUT_DIR to the airgapped host.

set -uo pipefail

# ---------------------------------------------------------------------------
# Configuration — leave blank to auto-detect latest; set to pin a version.
# ---------------------------------------------------------------------------
HARBOR_VERSION=""           # e.g. "v2.10.2"  — blank = latest
DOCKER_COMPOSE_VERSION=""   # e.g. "v2.23.0"  — blank = latest
PHOTON_OS_VERSION="5.0"     # Photon OS major version (5.0 or 4.0)
OUTPUT_DIR="/opt/packages"

# Base package names to resolve from the Photon OS repo
DOCKER_RPM_NAMES=(
    "docker"
    "containerd"
    "docker-cli"
    "libseccomp"
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PHOTON_REPO="https://packages.vmware.com/photon/${PHOTON_OS_VERSION}/photon_${PHOTON_OS_VERSION}_x86_64/x86_64"
FAILED=()

log()  { echo "[INFO]  $*"; }
warn() { echo "[WARN]  $*" >&2; }
err()  { echo "[ERROR] $*" >&2; exit 1; }

check_tools() {
    for tool in curl md5sum sort grep sed; do
        command -v "$tool" &>/dev/null || err "'$tool' is required but not installed."
    done
}

create_dirs() {
    mkdir -p "$OUTPUT_DIR"
    log "Output directory: $OUTPUT_DIR"
}

# Download url → dest; skip silently if file already exists.
download() {
    local url="$1" dest="$2" name
    name=$(basename "$dest")

    if [[ -f "$dest" ]]; then
        log "Skipping (already exists): $name"
        return 0
    fi

    log "Downloading: $name"
    if ! curl -fsSL --retry 3 --retry-delay 5 -o "$dest" "$url"; then
        warn "Failed to download: $name"
        rm -f "$dest"
        FAILED+=("$name")
        return 1
    fi
}

verify_md5() {
    local installer="$1" checksum_file="$2"
    log "Verifying MD5: $(basename "$installer")"
    (cd "$OUTPUT_DIR" && md5sum -c "$(basename "$checksum_file")") \
        || { warn "MD5 mismatch: $(basename "$installer")"; FAILED+=("$(basename "$installer") [md5]"); }
}

# ---------------------------------------------------------------------------
# Version resolution
# ---------------------------------------------------------------------------

fetch_github_latest() {
    local repo="$1"
    curl -fsSL "https://api.github.com/repos/${repo}/releases/latest" \
        | grep '"tag_name"' \
        | sed -E 's/.*"tag_name": "([^"]+)".*/\1/'
}

# Resolves the highest-versioned RPM filename for a package from the Photon OS repo index.
# Anchors match on <name>-<digit> to prevent docker matching docker-cli, etc.
fetch_latest_rpm() {
    local pkg="$1"
    curl -fsSL "${PHOTON_REPO}/" \
        | grep -oE "${pkg}-[0-9][^\"<> ]+\.x86_64\.rpm" \
        | sort -V | tail -1
}

resolve_versions() {
    if [[ -z "$HARBOR_VERSION" ]]; then
        log "Resolving latest Harbor version..."
        HARBOR_VERSION=$(fetch_github_latest "goharbor/harbor")
        [[ -n "$HARBOR_VERSION" ]] || err "Failed to resolve Harbor version."
    fi
    log "Harbor:         $HARBOR_VERSION"

    if [[ -z "$DOCKER_COMPOSE_VERSION" ]]; then
        log "Resolving latest docker-compose version..."
        DOCKER_COMPOSE_VERSION=$(fetch_github_latest "docker/compose")
        [[ -n "$DOCKER_COMPOSE_VERSION" ]] || err "Failed to resolve docker-compose version."
    fi
    log "docker-compose: $DOCKER_COMPOSE_VERSION"
}

resolve_rpm_filenames() {
    log "Resolving Docker RPM filenames from Photon OS ${PHOTON_OS_VERSION} repo..."
    RESOLVED_RPMS=()
    for pkg in "${DOCKER_RPM_NAMES[@]}"; do
        local rpm
        rpm=$(fetch_latest_rpm "$pkg") || true
        if [[ -z "$rpm" ]]; then
            warn "Could not resolve RPM for: $pkg — skipping."
            FAILED+=("${pkg}.rpm")
            continue
        fi
        log "  ${pkg} → ${rpm}"
        RESOLVED_RPMS+=("$rpm")
    done
}

# ---------------------------------------------------------------------------
# Download functions
# ---------------------------------------------------------------------------

download_harbor() {
    local base="https://github.com/goharbor/harbor/releases/download/${HARBOR_VERSION}"
    local installer="harbor-offline-installer-${HARBOR_VERSION}.tgz"

    download "${base}/${installer}" "${OUTPUT_DIR}/${installer}" || return
    download "${base}/md5sum"       "${OUTPUT_DIR}/md5sum"       || return
    verify_md5 "${OUTPUT_DIR}/${installer}" "${OUTPUT_DIR}/md5sum"
    log "Harbor offline installer ready."
}

download_docker_compose() {
    local binary="docker-compose-linux-x86_64"
    local url="https://github.com/docker/compose/releases/download/${DOCKER_COMPOSE_VERSION}/${binary}"

    download "$url" "${OUTPUT_DIR}/${binary}" || return
    chmod +x "${OUTPUT_DIR}/${binary}"
    log "docker-compose ready."
}

download_docker_rpms() {
    for rpm in "${RESOLVED_RPMS[@]}"; do
        download "${PHOTON_REPO}/${rpm}" "${OUTPUT_DIR}/${rpm}" || true
    done
    log "Docker RPMs done."
}

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

main() {
    check_tools
    create_dirs
    resolve_versions
    resolve_rpm_filenames
    download_harbor
    download_docker_compose
    download_docker_rpms

    if [[ ${#FAILED[@]} -gt 0 ]]; then
        warn "The following items failed and must be resolved before deployment:"
        for item in "${FAILED[@]}"; do
            warn "  - $item"
        done
        exit 1
    fi

    log "All prerequisites saved to: ${OUTPUT_DIR}"
    log "Transfer to the airgapped host, then run: sudo python3 deploy_harbor.py config.yaml"
}

main "$@"
