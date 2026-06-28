#!/usr/bin/env bash
# =========================================================
# TS Husky Translate Docker Restore Script for Ubuntu 24
# Restores one all-in-one archive created by ts_backup.ps1.
# =========================================================

set -Eeuo pipefail

TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DB_NAME="${DB_NAME:-husky-translate}"
DB_USER="${DB_USER:-odoo}"
DB_PASS="${DB_PASS:-odoo}"
ODOO_CONTAINER="${ODOO_CONTAINER:-husky_trans_web}"
DB_CONTAINER="${DB_CONTAINER:-husky_trans_db}"
PROJECT_ROOT="${PROJECT_ROOT:-/opt/husky-trans}"
ODOO_URL="${ODOO_URL:-http://localhost:8070}"
ODOO_BASE_IMAGE="${ODOO_BASE_IMAGE:-odoo:18.0}"
PIP_INDEX_URL="${PIP_INDEX_URL:-https://mirrors.aliyun.com/pypi/simple/}"
APT_MIRROR="${APT_MIRROR:-mirrors.aliyun.com}"
WORK_DIR="${WORK_DIR:-/tmp/ts_restore_${TIMESTAMP}}"
LOG_DIR="${LOG_DIR:-$SCRIPT_DIR/logs}"
LOG_FILE="$LOG_DIR/ts_restore_${TIMESTAMP}.log"
IMPORT_LOG="$LOG_DIR/db_import_${TIMESTAMP}.log"

ARCHIVE="${1:-}"
COMPOSE_CMD=""
ZIP_CMD=""
DB_RESTORE_OK=false
FILESTORE_RESTORE_OK=false
HEALTH_OK=false

mkdir -p "$LOG_DIR"

log_info()    { local msg="[$(date '+%Y-%m-%d %H:%M:%S')] [INFO] $*"; echo -e "\e[37m$msg\e[0m"; echo "$msg" >> "$LOG_FILE"; }
log_success() { local msg="[$(date '+%Y-%m-%d %H:%M:%S')] [OK] $*";   echo -e "\e[32m$msg\e[0m"; echo "$msg" >> "$LOG_FILE"; }
log_warn()    { local msg="[$(date '+%Y-%m-%d %H:%M:%S')] [WARN] $*"; echo -e "\e[33m$msg\e[0m"; echo "$msg" >> "$LOG_FILE"; }
log_error()   { local msg="[$(date '+%Y-%m-%d %H:%M:%S')] [FAIL] $*"; echo -e "\e[31m$msg\e[0m"; echo "$msg" >> "$LOG_FILE"; }
log_step()    { local msg="[$(date '+%Y-%m-%d %H:%M:%S')] ===== $* ====="; echo -e "\e[36m$msg\e[0m"; echo "$msg" >> "$LOG_FILE"; }

on_error() {
    local line="${1:-unknown}"
    log_error "Restore failed near line $line"
    log_info "Log file: $LOG_FILE"
}
trap 'on_error $LINENO' ERR

as_root() {
    if [ "$EUID" -ne 0 ]; then
        if command -v sudo >/dev/null 2>&1; then
            exec sudo -E bash "$0" "$ARCHIVE"
        fi
        log_error "Please run as root, for example: sudo bash $0 ${ARCHIVE:-/path/to/ts_backup.7z}"
        exit 1
    fi
}

find_archive() {
    if [ -n "$ARCHIVE" ]; then
        return
    fi

    ARCHIVE="$(ls -1t "$SCRIPT_DIR"/ts_backup_*.7z 2>/dev/null | head -n 1 || true)"
    if [ -z "$ARCHIVE" ]; then
        log_error "No archive specified and no ts_backup_*.7z found in $SCRIPT_DIR"
        exit 1
    fi
}

check_prerequisites() {
    log_step "1/9 Checking prerequisites"

    if ! docker info >/dev/null 2>&1; then
        log_error "Docker is not running or not accessible."
        exit 1
    fi
    log_success "Docker is available"

    if docker compose version >/dev/null 2>&1; then
        COMPOSE_CMD="docker compose"
    elif command -v docker-compose >/dev/null 2>&1; then
        COMPOSE_CMD="docker-compose"
    else
        log_error "Docker Compose was not found."
        exit 1
    fi
    log_success "Docker Compose is available: $COMPOSE_CMD"

    ZIP_CMD="$(command -v 7za || command -v 7z || command -v 7zz || true)"
    if [ -z "$ZIP_CMD" ]; then
        log_info "Installing 7-Zip extractor..."
        apt-get update 2>&1 | tee -a "$LOG_FILE"
        apt-get install -y p7zip-full 2>&1 | tee -a "$LOG_FILE" || apt-get install -y 7zip 2>&1 | tee -a "$LOG_FILE"
        ZIP_CMD="$(command -v 7za || command -v 7z || command -v 7zz || true)"
    fi
    if [ -z "$ZIP_CMD" ]; then
        log_error "Could not install or find 7-Zip extractor."
        exit 1
    fi
    log_success "7-Zip extractor is available: $ZIP_CMD"

    if ! command -v curl >/dev/null 2>&1; then
        log_info "Installing curl..."
        apt-get update 2>&1 | tee -a "$LOG_FILE"
        apt-get install -y curl 2>&1 | tee -a "$LOG_FILE"
    fi

    if [ ! -f "$ARCHIVE" ]; then
        log_error "Backup archive not found: $ARCHIVE"
        exit 1
    fi
    log_success "Backup archive found: $ARCHIVE"
}

extract_backup() {
    log_step "2/9 Extracting backup package"
    rm -rf "$WORK_DIR"
    mkdir -p "$WORK_DIR"
    "$ZIP_CMD" x "$ARCHIVE" -o"$WORK_DIR" -y >>"$LOG_FILE" 2>&1

    if [ ! -f "$WORK_DIR/project/docker-compose.yml" ]; then
        log_error "Archive does not contain project/docker-compose.yml"
        exit 1
    fi
    if [ ! -f "$WORK_DIR/db/${DB_NAME}.sql" ]; then
        log_error "Archive does not contain db/${DB_NAME}.sql"
        exit 1
    fi
    if [ ! -f "$WORK_DIR/filestore/filestore.tar.gz" ]; then
        log_warn "Archive does not contain filestore/filestore.tar.gz; filestore restore will be skipped"
    fi
    log_success "Backup package extracted to $WORK_DIR"
    log_info "Project files are staged at $WORK_DIR/project and will be copied to $PROJECT_ROOT in step 4"
}

stop_existing_stack() {
    log_step "3/9 Stopping existing stack"
    log_info "This step checks the old installed project at $PROJECT_ROOT, not the freshly extracted backup staging folder"
    if [ -f "$PROJECT_ROOT/docker-compose.yml" ]; then
        log_info "Stopping existing Docker Compose stack in $PROJECT_ROOT"
        cd "$PROJECT_ROOT"
        $COMPOSE_CMD down -v 2>&1 | tee -a "$LOG_FILE" || true
    else
        log_info "No existing docker-compose.yml found at $PROJECT_ROOT"
    fi
}

restore_project_files() {
    log_step "4/9 Restoring project files"
    local parent_dir
    parent_dir="$(dirname "$PROJECT_ROOT")"
    mkdir -p "$parent_dir"

    if [ -d "$PROJECT_ROOT" ]; then
        local old_path="${PROJECT_ROOT}.before_${TIMESTAMP}"
        log_info "Moving old project directory to $old_path"
        mv "$PROJECT_ROOT" "$old_path"
    fi

    mkdir -p "$PROJECT_ROOT"
    cp -a "$WORK_DIR/project/." "$PROJECT_ROOT/"

    log_info "Normalizing line endings and permissions"
    find "$PROJECT_ROOT" -type f \( \
        -name "*.py" -o -name "*.xml" -o -name "*.csv" -o -name "*.yml" -o -name "*.yaml" -o \
        -name "*.conf" -o -name "*.txt" -o -name "*.rst" -o -name "*.md" -o -name "*.js" -o \
        -name "*.css" -o -name "*.scss" -o -name "*.sh" -o -name "*.ps1" \
    \) -exec sed -i 's/\r$//' {} + 2>/dev/null || true

    chmod -R a+rX "$PROJECT_ROOT/config" "$PROJECT_ROOT/myaddons" 2>/dev/null || true

    touch "$PROJECT_ROOT/.env"
    for env_name in ODOO_BASE_IMAGE PIP_INDEX_URL APT_MIRROR; do
        env_value="${!env_name}"
        if grep -q "^${env_name}=" "$PROJECT_ROOT/.env"; then
            sed -i "s|^${env_name}=.*|${env_name}=${env_value}|" "$PROJECT_ROOT/.env"
        else
            printf '\n%s=%s\n' "$env_name" "$env_value" >> "$PROJECT_ROOT/.env"
        fi
    done

    log_success "Project restored to $PROJECT_ROOT"
}

start_database() {
    log_step "5/9 Starting database"
    cd "$PROJECT_ROOT"
    export ODOO_BASE_IMAGE
    $COMPOSE_CMD up -d db 2>&1 | tee -a "$LOG_FILE"

    log_info "Waiting for PostgreSQL readiness"
    local count=0
    until docker exec "$DB_CONTAINER" pg_isready -U "$DB_USER" -d postgres >/dev/null 2>&1; do
        count=$((count + 1))
        if [ "$count" -ge 45 ]; then
            log_error "PostgreSQL did not become ready in time"
            docker logs --tail 80 "$DB_CONTAINER" >>"$LOG_FILE" 2>&1 || true
            exit 1
        fi
        sleep 2
    done

    docker exec -u postgres "$DB_CONTAINER" psql -c "ALTER USER $DB_USER WITH SUPERUSER CREATEDB;" >>"$LOG_FILE" 2>&1 || true
    log_success "PostgreSQL is ready"
}

restore_database() {
    log_step "6/9 Restoring database"
    local sql_file="$WORK_DIR/db/${DB_NAME}.sql"
    docker cp "$sql_file" "${DB_CONTAINER}:/tmp/${DB_NAME}.sql"

    docker exec -e PGPASSWORD="$DB_PASS" "$DB_CONTAINER" psql -U "$DB_USER" -d postgres \
        -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='${DB_NAME}' AND pid <> pg_backend_pid();" >>"$LOG_FILE" 2>&1 || true

    log_info "Importing SQL. Detailed log: $IMPORT_LOG"
    docker exec -e PGPASSWORD="$DB_PASS" "$DB_CONTAINER" psql -U "$DB_USER" -d postgres \
        -f "/tmp/${DB_NAME}.sql" >"$IMPORT_LOG" 2>&1

    docker exec "$DB_CONTAINER" rm -f "/tmp/${DB_NAME}.sql" >/dev/null 2>&1 || true

    local table_count
    table_count="$(docker exec -e PGPASSWORD="$DB_PASS" "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -t \
        -c "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public';" 2>/dev/null | tr -d '[:space:]')"

    if [[ "$table_count" =~ ^[0-9]+$ ]] && [ "$table_count" -gt 0 ]; then
        docker exec -e PGPASSWORD="$DB_PASS" "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" \
            -c "DELETE FROM ir_attachment WHERE url LIKE '/web/assets/%';" >>"$LOG_FILE" 2>&1 || true
        DB_RESTORE_OK=true
        log_success "Database restored. Public table count: $table_count"
    else
        log_error "Database verification failed. Table count: ${table_count:-empty}"
        tail -40 "$IMPORT_LOG" || true
        exit 1
    fi
}

start_web() {
    log_step "7/9 Building and starting Odoo web"
    cd "$PROJECT_ROOT"
    export ODOO_BASE_IMAGE
    export PIP_INDEX_URL
    export APT_MIRROR
    log_info "Using ODOO_BASE_IMAGE=$ODOO_BASE_IMAGE"
    log_info "Using PIP_INDEX_URL=$PIP_INDEX_URL"
    log_info "Using APT_MIRROR=$APT_MIRROR"
    $COMPOSE_CMD up -d --build web 2>&1 | tee -a "$LOG_FILE"

    local count=0
    until docker inspect -f '{{.State.Running}}' "$ODOO_CONTAINER" 2>/dev/null | grep -q true; do
        count=$((count + 1))
        if [ "$count" -ge 45 ]; then
            log_error "Odoo web container did not start in time"
            docker logs --tail 120 "$ODOO_CONTAINER" >>"$LOG_FILE" 2>&1 || true
            exit 1
        fi
        sleep 2
    done
    log_success "Odoo web container is running"
}

restore_filestore() {
    log_step "8/9 Restoring filestore"
    local fs_archive="$WORK_DIR/filestore/filestore.tar.gz"
    if [ ! -f "$fs_archive" ]; then
        log_warn "Filestore archive missing; skipped"
        return 0
    fi

    docker cp "$fs_archive" "${ODOO_CONTAINER}:/tmp/filestore.tar.gz"
    docker exec -u root -e DB_NAME="$DB_NAME" "$ODOO_CONTAINER" bash -lc '
set -e
mkdir -p /var/lib/odoo
rm -rf "/var/lib/odoo/filestore/${DB_NAME}"
tar -xzf /tmp/filestore.tar.gz -C /var/lib/odoo
mkdir -p "/var/lib/odoo/filestore/${DB_NAME}"
if id odoo >/dev/null 2>&1; then
    chown -R odoo:odoo /var/lib/odoo
fi
find /var/lib/odoo -type d -exec chmod 755 {} +
find /var/lib/odoo -type f -exec chmod 644 {} +
rm -f /tmp/filestore.tar.gz
' >>"$LOG_FILE" 2>&1

    docker restart "$ODOO_CONTAINER" >>"$LOG_FILE" 2>&1
    FILESTORE_RESTORE_OK=true
    log_success "Filestore restored and Odoo restarted"
}

check_health() {
    log_step "9/9 Checking service health"
    local code="000"
    for i in $(seq 1 24); do
        sleep 5
        code="$(curl -s -o /dev/null -w "%{http_code}" --max-time 8 "$ODOO_URL" || echo "000")"
        if [[ "$code" =~ ^(200|302|303)$ ]]; then
            HEALTH_OK=true
            log_success "Odoo responded successfully: HTTP $code"
            return 0
        fi
        log_info "Waiting for Odoo ($i/24), HTTP=$code"
    done

    log_warn "Odoo did not pass HTTP health check. Recent container logs:"
    docker logs --tail 80 "$ODOO_CONTAINER" 2>&1 | tee -a "$LOG_FILE" || true
    return 1
}

print_summary() {
    local db_status="failed"
    local fs_status="skipped"
    local health_status="failed"
    [ "$DB_RESTORE_OK" = true ] && db_status="restored"
    [ "$FILESTORE_RESTORE_OK" = true ] && fs_status="restored"
    [ "$HEALTH_OK" = true ] && health_status="healthy"

    cat <<REPORT

=========================================================
TS Husky Translate restore summary
=========================================================
Archive      : $ARCHIVE
Project root : $PROJECT_ROOT
Database     : $DB_NAME ($db_status)
Filestore    : $fs_status
Service      : $health_status
URL          : $ODOO_URL
Log file     : $LOG_FILE
Import log   : $IMPORT_LOG

Common commands:
  cd $PROJECT_ROOT && docker compose ps
  cd $PROJECT_ROOT && docker compose logs -f web
  cd $PROJECT_ROOT && docker compose restart
=========================================================
REPORT
}

main() {
    find_archive
    as_root

    log_info "TS Husky Translate restore started"
    log_info "Archive: $ARCHIVE"
    log_info "Project root: $PROJECT_ROOT"
    log_info "Database: $DB_NAME"

    check_prerequisites
    extract_backup
    stop_existing_stack
    restore_project_files
    start_database
    restore_database
    start_web
    restore_filestore
    check_health || true
    rm -rf "$WORK_DIR"
    print_summary | tee -a "$LOG_FILE"

    if [ "$HEALTH_OK" = true ]; then
        exit 0
    fi
    exit 1
}

main "$@"
