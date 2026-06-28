#!/usr/bin/env bash
# =========================================================
# HUSKY TRANSLATE - Restore Script for CentOS / RHEL 8+
# Usage: sudo bash ts_restore_centos.sh <backup.7z>
# =========================================================

set -Eeuo pipefail

ARCHIVE="${1:-}"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Note: onlyoffice_lib (document cache 500MB+) and onlyoffice_logs are
# intentionally NOT backed up - they regenerate automatically on first use.
PROJECT_ROOT="${PROJECT_ROOT:-/opt/husky-trans}"
WORK_DIR="${WORK_DIR:-/tmp/husky_restore_${TIMESTAMP}}"
LOG_DIR="${LOG_DIR:-$SCRIPT_DIR/logs}"
LOG_FILE="$LOG_DIR/husky_restore_${TIMESTAMP}.log"
PORT_PORTAL="${PORT_PORTAL:-8070}"
PORT_ONLYOFFICE="${PORT_ONLYOFFICE:-8090}"
PORTAL_CONTAINER="${PORTAL_CONTAINER:-husky_portal}"
SERVER_IP="${SERVER_IP:-}"

mkdir -p "$LOG_DIR"

# ---- Logging ----
log_info()    { echo -e "\e[37m[$(date '+%H:%M:%S')] [INFO] $*\e[0m"; echo "$(date '+%Y-%m-%d %H:%M:%S') [INFO] $*" >> "$LOG_FILE"; }
log_ok()      { echo -e "\e[32m[$(date '+%H:%M:%S')] [ OK ] $*\e[0m"; echo "$(date '+%Y-%m-%d %H:%M:%S') [ OK ] $*" >> "$LOG_FILE"; }
log_warn()    { echo -e "\e[33m[$(date '+%H:%M:%S')] [WARN] $*\e[0m"; echo "$(date '+%Y-%m-%d %H:%M:%S') [WARN] $*" >> "$LOG_FILE"; }
log_err()     { echo -e "\e[31m[$(date '+%H:%M:%S')] [FAIL] $*\e[0m"; echo "$(date '+%Y-%m-%d %H:%M:%S') [FAIL] $*" >> "$LOG_FILE"; }

# ---- Pre-flight checks ----
check_cmd() {
    if ! command -v "$1" &>/dev/null; then
        log_err "Missing command: $1 - install it first"
        exit 1
    fi
}

find_archive() {
    if [ -n "$ARCHIVE" ] && [ -f "$ARCHIVE" ]; then return; fi
    # Auto-detect most recent backup
    local latest
    latest=$(ls -t husky_trans_backup_*.7z 2>/dev/null | head -1)
    if [ -n "$latest" ]; then
        ARCHIVE="$latest"
        log_info "Auto-detected archive: $ARCHIVE"
    else
        log_err "No backup archive specified and none found in current directory."
        log_err "Usage: sudo bash $0 <backup.7z>"
        exit 1
    fi
}

install_deps_centos() {
    log_info "Installing dependencies..."
    yum install -y epel-release 2>/dev/null || true
    yum install -y p7zip p7zip-plugins tar curl 2>/dev/null || {
        log_warn "p7zip not in repo, trying 7zip..."
        yum install -y https://dl.fedoraproject.org/pub/epel/epel-release-latest-8.noarch.rpm 2>/dev/null || true
        yum install -y p7zip p7zip-plugins 2>/dev/null || true
    }
    log_ok "Dependencies installed"
}

install_docker_centos() {
    if command -v docker &>/dev/null; then
        log_info "Docker already installed: $(docker --version)"
        return
    fi
    log_info "Installing Docker Engine..."
    yum remove -y docker docker-client docker-common docker-latest 2>/dev/null || true
    yum install -y yum-utils
    yum-config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo 2>/dev/null || true
    yum install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
    systemctl enable docker --now
    log_ok "Docker installed"
}

# ======================== MAIN ========================

log_info "========================================="
log_info "  HUSKY TRANSLATE Restore (CentOS)"
log_info "========================================="

find_archive
log_info "Archive  : $ARCHIVE"
log_info "Project  : $PROJECT_ROOT"

# Step 1: Install dependencies
install_deps_centos
install_docker_centos

# Step 2: Extract backup
log_info "Extracting backup archive..."
rm -rf "$WORK_DIR"
mkdir -p "$WORK_DIR"
7za x "$ARCHIVE" -o"$WORK_DIR" -y
log_ok "Extracted to $WORK_DIR"

# Step 3: Stop old project (if running)
if [ -d "$PROJECT_ROOT" ] && [ -f "$PROJECT_ROOT/docker-compose.yml" ]; then
    log_info "Stopping old husky-trans stack..."
    cd "$PROJECT_ROOT" && docker compose down 2>/dev/null || true
fi

# Step 3.5: Force-remove old portal container to ensure clean bind-mount recreation
log_info "Removing old portal container (if any)..."
docker rm -f "$PORTAL_CONTAINER" 2>/dev/null || true
log_ok "Old portal container cleaned"

# Step 4: Copy project files
log_info "Copying project files to $PROJECT_ROOT..."
BACKUP_PROJ=$(find "$WORK_DIR" -name "project" -type d | head -1)
if [ -z "$BACKUP_PROJ" ]; then
    log_err "Project directory not found in backup archive"
    exit 1
fi
rm -rf "$PROJECT_ROOT"
cp -r "$BACKUP_PROJ" "$PROJECT_ROOT"

# Step 4.5: Fix permissions (nginx runs as non-root, needs traverse access)
log_info "Fixing file permissions for nginx access..."
chmod -R a+rX "$PROJECT_ROOT" 2>/dev/null || true
log_ok "Permissions fixed"

# Step 4.6: Verify portal files exist (required for bind mount)
if [ -f "$PROJECT_ROOT/husky.html" ]; then
    log_ok "Portal entry point found: $PROJECT_ROOT/husky.html"
else
    log_warn "husky.html not found at $PROJECT_ROOT/husky.html - portal may not render correctly"
fi

# Step 5: Restore Docker volumes
log_info "Restoring Docker volumes..."
BACKUP_VOLS=$(find "$WORK_DIR" -name "volumes" -type d | head -1)
if [ -n "$BACKUP_VOLS" ]; then
    for vol_tar in "$BACKUP_VOLS"/*.tar.gz; do
        [ -f "$vol_tar" ] || continue
        vol_name=$(basename "$vol_tar" .tar.gz)
        log_info "  Restoring volume: $vol_name"
        docker volume create "$vol_name" 2>/dev/null || true
        docker run --rm -v "${vol_name}:/dest" -v "$BACKUP_VOLS:/src" alpine sh -c "tar xzf /src/${vol_name}.tar.gz -C /dest" 2>&1 || log_warn "  WARN: $vol_name restore had issues"
        log_ok "  $vol_name restored"
    done
else
    log_warn "No volumes directory found in backup - volumes will be fresh"
fi

# Step 5.5: Configure Cloudreve OnlyOffice WOPI (external URL for browser access)
CLOUDREVE_CONF="$PROJECT_ROOT/cloudreve_data/conf.ini"
if [ -f "$CLOUDREVE_CONF" ]; then
    if [ -n "$SERVER_IP" ]; then
        log_info "Configuring Cloudreve WOPI for external OnlyOffice access: http://$SERVER_IP:$PORT_ONLYOFFICE"
        sed -i "s|DiscoveryUrl = .*|DiscoveryUrl = http://$SERVER_IP:$PORT_ONLYOFFICE/hosting/discovery|" "$CLOUDREVE_CONF"
        log_ok "Cloudreve WOPI DiscoveryUrl set to external IP"
    else
        log_info "SERVER_IP not set, using host.docker.internal for local access"
        sed -i "s|DiscoveryUrl = .*|DiscoveryUrl = http://host.docker.internal:$PORT_ONLYOFFICE/hosting/discovery|" "$CLOUDREVE_CONF"
        log_ok "Cloudreve WOPI DiscoveryUrl set to host.docker.internal:$PORT_ONLYOFFICE"
    fi
fi

# Step 6: Pull images & start (bind-mount ensures host files are served directly)
log_info "Pulling Docker images..."
cd "$PROJECT_ROOT"
docker compose pull 2>/dev/null || true
log_info "Building and starting husky-trans (portal bind-mount: $PROJECT_ROOT -> /usr/share/nginx/html)..."
docker compose up -d --build 2>&1 | tee -a "$LOG_FILE"

# Step 7: Wait for services
log_info "Waiting for services to become healthy (up to 90s)..."
PORTAL_OK=false
OO_OK=false
for i in $(seq 1 18); do
    sleep 5
    if curl -sf "http://localhost:$PORT_PORTAL/husky.html" >/dev/null 2>&1; then
        PORTAL_OK=true
        log_ok "Portal (bind-mount) responding at :$PORT_PORTAL"
    fi
    if curl -sf "http://localhost:$PORT_ONLYOFFICE/welcome/" >/dev/null 2>&1; then
        OO_OK=true
    fi
    [ "$PORTAL_OK" = true ] && [ "$OO_OK" = true ] && break
done

# Step 8: Summary
cat <<EOF

=========================================================
  HUSKY TRANSLATE - Restore Complete
=========================================================
Project  : $PROJECT_ROOT
Portal   : http://localhost:$PORT_PORTAL
OnlyOffice : http://localhost:$PORT_ONLYOFFICE
$( [ -n "$SERVER_IP" ] && echo "Public OO  : http://$SERVER_IP:$PORT_ONLYOFFICE" || echo "Public OO  : (set SERVER_IP to configure)" )
Log      : $LOG_FILE

Status:
  Portal     (:$PORT_PORTAL)    : ${PORTAL_OK:-NOT READY}
  OnlyOffice (:$PORT_ONLYOFFICE): ${OO_OK:-NOT READY}

Bind-mount: $PROJECT_ROOT -> container:/usr/share/nginx/html (ro)

Common commands:
  cd $PROJECT_ROOT && docker compose ps
  cd $PROJECT_ROOT && docker compose logs -f
  cd $PROJECT_ROOT && docker compose restart
=========================================================

EOF

log_info "Restore finished."