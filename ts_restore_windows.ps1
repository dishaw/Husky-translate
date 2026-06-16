# =========================================================
# TS Odoo Translate Docker Restore Script for Windows
# Restores one all-in-one archive created by ts_backup.ps1.
# =========================================================

[CmdletBinding()]
param(
    [string]$Archive = "",
    [string]$ProjectRoot = $PSScriptRoot,
    [string]$BackupRoot = "D:\odoo-trans-backups",
    [string]$WorkRoot = (Join-Path $env:TEMP "ts_restore"),
    [string]$DbName = "odoo-translate",
    [string]$DbUser = "odoo",
    [string]$DbPass = "odoo",
    [string]$OdooContainer = "odoo_trans_web",
    [string]$DbContainer = "odoo_trans_db",
    [string]$OdooUrl = "http://localhost:8070",
    [string]$OdooBaseImage = "odoo:18.0",
    [string]$PipIndexUrl = "https://mirrors.aliyun.com/pypi/simple/",
    [string]$AptMirror = "",
    [switch]$Force,
    [switch]$SkipProjectFiles,
    [switch]$SkipFilestore
)

$ErrorActionPreference = "Stop"

$Timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$ScriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
$LogDir = Join-Path $ScriptDir "logs"
$LogFile = Join-Path $LogDir "ts_restore_windows_$Timestamp.log"
$ImportLog = Join-Path $LogDir "db_import_windows_$Timestamp.log"
$WorkDir = Join-Path $WorkRoot "ts_restore_$Timestamp"

$DbRestoreOk = $false
$FilestoreRestoreOk = $false
$HealthOk = $false

function Write-Log {
    param(
        [string]$Message,
        [string]$Level = "INFO",
        [ConsoleColor]$Color = [ConsoleColor]::Gray
    )
    $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] [$Level] $Message"
    Write-Host $line -ForegroundColor $Color
    Add-Content -LiteralPath $LogFile -Value $line -Encoding UTF8
}

function Write-Step {
    param([string]$Message)
    Write-Log "===== $Message =====" "STEP" Cyan
}

function Require-Command {
    param([string]$Name)
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required command not found: $Name"
    }
}

function Get-7Zip {
    $default7z = "C:\Program Files\7-Zip\7z.exe"
    if (Test-Path $default7z) {
        return $default7z
    }

    $cmd = Get-Command "7z.exe" -ErrorAction SilentlyContinue
    if ($cmd) {
        return $cmd.Source
    }

    throw "7-Zip was not found. Install 7-Zip or add 7z.exe to PATH."
}

function Invoke-Checked {
    param(
        [string]$Description,
        [scriptblock]$Script
    )

    Write-Log $Description "INFO" DarkCyan
    & $Script 2>&1 | Tee-Object -FilePath $LogFile -Append
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE"
    }
}

function Invoke-CheckedToFile {
    param(
        [string]$Description,
        [string]$OutputFile,
        [scriptblock]$Script
    )

    Write-Log "$Description. Detailed log: $OutputFile" "INFO" DarkCyan
    & $Script > $OutputFile 2>&1
    if ($LASTEXITCODE -ne 0) {
        Get-Content -LiteralPath $OutputFile -Tail 80 -ErrorAction SilentlyContinue | ForEach-Object {
            Write-Log $_ "IMPORT" DarkGray
        }
        throw "$Description failed with exit code $LASTEXITCODE"
    }
}

function Find-Archive {
    if ($Archive) {
        return (Resolve-Path -LiteralPath $Archive).Path
    }

    $candidates = @()
    if (Test-Path $ScriptDir) {
        $candidates += Get-ChildItem -LiteralPath $ScriptDir -File -Filter "ts_backup_*.7z" -ErrorAction SilentlyContinue
    }
    if (Test-Path $BackupRoot) {
        $candidates += Get-ChildItem -LiteralPath $BackupRoot -File -Filter "ts_backup_*.7z" -ErrorAction SilentlyContinue
    }

    $latest = $candidates | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $latest) {
        throw "No archive specified and no ts_backup_*.7z found in $ScriptDir or $BackupRoot"
    }
    return $latest.FullName
}

function Confirm-Restore {
    if ($Force) {
        return
    }

    Write-Host ""
    Write-Host "This restore will stop the Docker stack, remove compose volumes, restore the SQL database, and copy project files." -ForegroundColor Yellow
    Write-Host "ProjectRoot: $ProjectRoot" -ForegroundColor Yellow
    Write-Host "Database   : $DbName" -ForegroundColor Yellow
    $answer = Read-Host "Type RESTORE to continue"
    if ($answer -ne "RESTORE") {
        throw "Restore cancelled by user."
    }
}

function Check-Prerequisites {
    Write-Step "1/9 Checking prerequisites"
    Require-Command "docker"
    Require-Command "robocopy"

    docker info *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "Docker is not running or not accessible. Start Docker Desktop and try again."
    }

    docker compose version *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "Docker Compose was not found. This script expects Docker Desktop with 'docker compose'."
    }

    $script:ZipExe = Get-7Zip
    if (-not (Test-Path -LiteralPath $script:ArchivePath)) {
        throw "Backup archive not found: $script:ArchivePath"
    }

    Write-Log "Docker is available" "OK" Green
    Write-Log "Docker Compose is available" "OK" Green
    Write-Log "7-Zip extractor: $script:ZipExe" "OK" Green
    Write-Log "Backup archive: $script:ArchivePath" "OK" Green
}

function Extract-Backup {
    Write-Step "2/9 Extracting backup package"
    if (Test-Path -LiteralPath $WorkDir) {
        Remove-Item -LiteralPath $WorkDir -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null

    Invoke-Checked "Extracting archive" {
        & $script:ZipExe x $script:ArchivePath "-o$WorkDir" -y
    }

    $composeFile = Join-Path $WorkDir "project\docker-compose.yml"
    $sqlFile = Join-Path $WorkDir "db\$DbName.sql"
    if (-not (Test-Path -LiteralPath $composeFile)) {
        throw "Archive does not contain project/docker-compose.yml"
    }
    if (-not (Test-Path -LiteralPath $sqlFile)) {
        throw "Archive does not contain db/$DbName.sql"
    }

    $fsArchive = Join-Path $WorkDir "filestore\filestore.tar.gz"
    if (-not (Test-Path -LiteralPath $fsArchive)) {
        Write-Log "Archive does not contain filestore/filestore.tar.gz; filestore restore will be skipped" "WARN" Yellow
    }

    Write-Log "Backup extracted to $WorkDir" "OK" Green
}

function Stop-ExistingStack {
    Write-Step "3/9 Stopping existing stack"
    $composeFile = Join-Path $ProjectRoot "docker-compose.yml"
    if (-not (Test-Path -LiteralPath $composeFile)) {
        Write-Log "No existing docker-compose.yml found at $ProjectRoot" "INFO" Gray
        return
    }

    Push-Location $ProjectRoot
    try {
        Invoke-Checked "Stopping existing Docker Compose stack and removing volumes" {
            docker compose down -v
        }
    } finally {
        Pop-Location
    }
}

function Restore-ProjectFiles {
    Write-Step "4/9 Restoring project files"
    if ($SkipProjectFiles) {
        Write-Log "Skipped by -SkipProjectFiles" "WARN" Yellow
        return
    }

    $projectStage = Join-Path $WorkDir "project"
    $parent = Split-Path -Parent $ProjectRoot
    if (-not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
    }

    if (Test-Path -LiteralPath $ProjectRoot) {
        $oldPath = "$ProjectRoot.before_$Timestamp"
        Write-Log "Copying current project to $oldPath before overwriting" "INFO" DarkCyan
        & robocopy $ProjectRoot $oldPath /MIR /XD ".git" "__pycache__" "logs" /XF "*.pyc" "*.log" /R:2 /W:2 /NFL /NDL | Out-Host
        if ($LASTEXITCODE -gt 7) {
            throw "Robocopy backup failed with exit code $LASTEXITCODE"
        }
    } else {
        New-Item -ItemType Directory -Force -Path $ProjectRoot | Out-Null
    }

    Write-Log "Copying backup project files to $ProjectRoot" "INFO" DarkCyan
    & robocopy $projectStage $ProjectRoot /MIR /XD ".git" ".waylog" "__pycache__" "logs" /XF "*.pyc" "*.log" "ts_restore_windows.ps1" /R:2 /W:2 /NFL /NDL | Out-Host
    if ($LASTEXITCODE -gt 7) {
        throw "Robocopy restore failed with exit code $LASTEXITCODE"
    }

    $envFile = Join-Path $ProjectRoot ".env"
    Set-Content -LiteralPath $envFile -Value @(
        "ODOO_BASE_IMAGE=$OdooBaseImage",
        "PIP_INDEX_URL=$PipIndexUrl",
        "APT_MIRROR=$AptMirror"
    ) -Encoding UTF8

    Write-Log "Project restored to $ProjectRoot" "OK" Green
}

function Start-Database {
    Write-Step "5/9 Starting database"
    Push-Location $ProjectRoot
    try {
        $env:ODOO_BASE_IMAGE = $OdooBaseImage
        $env:PIP_INDEX_URL = $PipIndexUrl
        $env:APT_MIRROR = $AptMirror
        Invoke-Checked "Starting PostgreSQL service" {
            docker compose up -d db
        }
    } finally {
        Pop-Location
    }

    Write-Log "Waiting for PostgreSQL readiness" "INFO" DarkCyan
    for ($i = 1; $i -le 45; $i++) {
        docker exec $DbContainer pg_isready -U $DbUser -d postgres *> $null
        if ($LASTEXITCODE -eq 0) {
            Write-Log "PostgreSQL is ready" "OK" Green
            docker exec -u postgres $DbContainer psql -c "ALTER USER $DbUser WITH SUPERUSER CREATEDB;" *> $null
            return
        }
        Start-Sleep -Seconds 2
    }
    docker logs --tail 80 $DbContainer 2>&1 | Tee-Object -FilePath $LogFile -Append
    throw "PostgreSQL did not become ready in time"
}

function Restore-Database {
    Write-Step "6/9 Restoring database"
    $sqlFile = Join-Path $WorkDir "db\$DbName.sql"
    Invoke-Checked "Copying SQL dump into database container" {
        docker cp $sqlFile "${DbContainer}:/tmp/${DbName}.sql"
    }

    docker exec -e "PGPASSWORD=$DbPass" $DbContainer psql -U $DbUser -d postgres -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='$DbName' AND pid <> pg_backend_pid();" *> $null

    Invoke-CheckedToFile "Importing SQL dump" $ImportLog {
        docker exec -e "PGPASSWORD=$DbPass" $DbContainer psql -U $DbUser -d postgres -f "/tmp/${DbName}.sql"
    }

    docker exec $DbContainer rm -f "/tmp/${DbName}.sql" *> $null

    $tableCount = docker exec -e "PGPASSWORD=$DbPass" $DbContainer psql -U $DbUser -d $DbName -t -c "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public';"
    $tableCount = ($tableCount -join "").Trim()
    if ($tableCount -notmatch "^\d+$" -or [int]$tableCount -le 0) {
        throw "Database verification failed. Table count: $tableCount"
    }

    docker exec -e "PGPASSWORD=$DbPass" $DbContainer psql -U $DbUser -d $DbName -c "DELETE FROM ir_attachment WHERE url LIKE '/web/assets/%';" *> $null
    $script:DbRestoreOk = $true
    Write-Log "Database restored. Public table count: $tableCount" "OK" Green
}

function Start-Web {
    Write-Step "7/9 Building and starting Odoo web"
    Push-Location $ProjectRoot
    try {
        $env:ODOO_BASE_IMAGE = $OdooBaseImage
        $env:PIP_INDEX_URL = $PipIndexUrl
        $env:APT_MIRROR = $AptMirror
        Write-Log "Using ODOO_BASE_IMAGE=$OdooBaseImage" "INFO" Gray
        Write-Log "Using PIP_INDEX_URL=$PipIndexUrl" "INFO" Gray
        Write-Log "Using APT_MIRROR=$AptMirror" "INFO" Gray
        Invoke-Checked "Building and starting Odoo web" {
            docker compose up -d --build web
        }
    } finally {
        Pop-Location
    }

    for ($i = 1; $i -le 45; $i++) {
        $running = docker inspect -f "{{.State.Running}}" $OdooContainer 2>$null
        if ($running -eq "true") {
            Write-Log "Odoo web container is running" "OK" Green
            return
        }
        Start-Sleep -Seconds 2
    }
    docker logs --tail 120 $OdooContainer 2>&1 | Tee-Object -FilePath $LogFile -Append
    throw "Odoo web container did not start in time"
}

function Restore-Filestore {
    Write-Step "8/9 Restoring filestore"
    if ($SkipFilestore) {
        Write-Log "Skipped by -SkipFilestore" "WARN" Yellow
        return
    }

    $fsArchive = Join-Path $WorkDir "filestore\filestore.tar.gz"
    if (-not (Test-Path -LiteralPath $fsArchive)) {
        Write-Log "Filestore archive missing; skipped" "WARN" Yellow
        return
    }

    Invoke-Checked "Copying filestore archive into Odoo container" {
        docker cp $fsArchive "${OdooContainer}:/tmp/filestore.tar.gz"
    }

    $restoreCmd = @"
set -e
mkdir -p /var/lib/odoo
rm -rf "/var/lib/odoo/filestore/${DbName}"
tar -xzf /tmp/filestore.tar.gz -C /var/lib/odoo
mkdir -p "/var/lib/odoo/filestore/${DbName}"
if id odoo >/dev/null 2>&1; then chown -R odoo:odoo /var/lib/odoo; fi
find /var/lib/odoo -type d -exec chmod 755 {} +
find /var/lib/odoo -type f -exec chmod 644 {} +
rm -f /tmp/filestore.tar.gz
"@

    Invoke-Checked "Restoring filestore inside Odoo container" {
        docker exec -u root -e "DB_NAME=$DbName" $OdooContainer bash -lc $restoreCmd
    }

    Invoke-Checked "Restarting Odoo after filestore restore" {
        docker restart $OdooContainer
    }

    $script:FilestoreRestoreOk = $true
    Write-Log "Filestore restored" "OK" Green
}

function Check-Health {
    Write-Step "9/9 Checking service health"
    for ($i = 1; $i -le 24; $i++) {
        Start-Sleep -Seconds 5
        try {
            $response = Invoke-WebRequest -Uri $OdooUrl -UseBasicParsing -TimeoutSec 8 -ErrorAction SilentlyContinue
            $code = [int]$response.StatusCode
        } catch {
            $code = if ($_.Exception.Response) { [int]$_.Exception.Response.StatusCode } else { 0 }
        }

        if ($code -in 200, 302, 303) {
            $script:HealthOk = $true
            Write-Log "Odoo responded successfully: HTTP $code" "OK" Green
            return
        }
        Write-Log "Waiting for Odoo ($i/24), HTTP=$code" "INFO" Gray
    }

    Write-Log "Odoo did not pass HTTP health check. Recent container logs:" "WARN" Yellow
    docker logs --tail 80 $OdooContainer 2>&1 | Tee-Object -FilePath $LogFile -Append
}

function Print-Summary {
    $dbStatus = if ($DbRestoreOk) { "restored" } else { "failed" }
    $fsStatus = if ($FilestoreRestoreOk) { "restored" } elseif ($SkipFilestore) { "skipped" } else { "skipped/failed" }
    $healthStatus = if ($HealthOk) { "healthy" } else { "failed" }

    $summary = @"

=========================================================
TS Odoo Translate restore summary
=========================================================
Archive      : $script:ArchivePath
Project root : $ProjectRoot
Database     : $DbName ($dbStatus)
Filestore    : $fsStatus
Service      : $healthStatus
URL          : $OdooUrl
Log file     : $LogFile
Import log   : $ImportLog

Common commands:
  cd "$ProjectRoot"
  docker compose ps
  docker compose logs -f web
  docker compose restart
=========================================================
"@
    Write-Host $summary
    Add-Content -LiteralPath $LogFile -Value $summary -Encoding UTF8
}

try {
    New-Item -ItemType Directory -Force -Path $LogDir, $WorkRoot | Out-Null
    $script:ArchivePath = Find-Archive

    Write-Log "TS Odoo Translate Windows restore started" "INFO" Green
    Write-Log "Archive: $script:ArchivePath"
    Write-Log "Project root: $ProjectRoot"
    Write-Log "Database: $DbName"

    Confirm-Restore
    Check-Prerequisites
    Extract-Backup
    Stop-ExistingStack
    Restore-ProjectFiles
    Start-Database
    Restore-Database
    Start-Web
    Restore-Filestore
    Check-Health
    Remove-Item -LiteralPath $WorkDir -Recurse -Force -ErrorAction SilentlyContinue
    Print-Summary

    if ($HealthOk) {
        exit 0
    }
    exit 1
} catch {
    Write-Log "Restore failed: $($_.Exception.Message)" "FAIL" Red
    Write-Log "Work folder kept for inspection: $WorkDir" "WARN" Yellow
    Write-Log "Log file: $LogFile" "INFO" Yellow
    Print-Summary
    exit 1
}
