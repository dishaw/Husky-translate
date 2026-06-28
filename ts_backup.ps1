# =========================================================
# HUSKY TRANSLATE - Backup Script
# Archives the project + Docker volumes into one 7z file.
# =========================================================

[CmdletBinding()]
param(
    [string]$ProjectRoot   = "D:\Husky-trans",
    [string]$BackupRoot    = "D:\Husky-trans-backups",
    [string]$LocalTemp     = "D:\Husky_Trans_Temp_Backup",
    [int]$RetentionDays    = 7
)

$ErrorActionPreference = "Stop"

$Date         = Get-Date -Format "yyyyMMdd_HHmmss"
$BackupName   = "husky_trans_backup_$Date"
$StageRoot    = Join-Path $LocalTemp $BackupName
$ProjectStage = Join-Path $StageRoot "project"
$VolStage     = Join-Path $StageRoot "volumes"
$MetadataFile = Join-Path $StageRoot "metadata.txt"
$ArchiveFile  = Join-Path $BackupRoot "$BackupName.7z"

# ---- OnlyOffice Docker volumes to back up ----
# skip: onlyoffice_lib (document cache, 500MB+ regenerable)
# skip: onlyoffice_logs (log files)
$Volumes = @(
    "husky-trans_onlyoffice_fonts",
    "husky-trans_onlyoffice_data"
)

# ---- Files/dirs to EXCLUDE from project copy ----
$ExcludeDirs  = @(".git", "__pycache__", "logs", "myaddons", "config")
$ExcludeFiles = @("*.log", "*.pyc", "*.7z", ".dockerignore", "*.mhtml")

function Get-7Zip {
    $default7z = "C:\Program Files\7-Zip\7z.exe"
    if (Test-Path $default7z) { return $default7z }
    $cmd = Get-Command "7z.exe" -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    throw "7-Zip not found. Install 7-Zip or add 7z.exe to PATH."
}

function Invoke-Cmd {
    param([string]$Cmd, [string]$Desc)
    Write-Host "[ ] $Desc" -ForegroundColor Cyan
    cmd /c $Cmd
    if ($LASTEXITCODE -ne 0) { throw "$Desc FAILED (exit $LASTEXITCODE)" }
    Write-Host "[OK] $Desc" -ForegroundColor Green
}

# ======================== MAIN ========================

$ZipExe = Get-7Zip

New-Item -ItemType Directory -Force -Path $LocalTemp, $BackupRoot | Out-Null
if (Test-Path $StageRoot) { Remove-Item -LiteralPath $StageRoot -Recurse -Force }
New-Item -ItemType Directory -Force -Path $ProjectStage, $VolStage | Out-Null

try {
    Write-Host "=================================================" -ForegroundColor Green
    Write-Host "  HUSKY TRANSLATE Backup"
    Write-Host "  $Date"
    Write-Host "=================================================" -ForegroundColor Green

    if (-not (Test-Path $ProjectRoot)) {
        throw "Project root not found: $ProjectRoot"
    }

    # [1/3] Copy project files
    Write-Host "`n[1/3] Copying project files (excl. volumes)..." -ForegroundColor Yellow
    $excludeArg = ""
    foreach ($d in $ExcludeDirs)  { $excludeArg += " /xd `"$d`"" }
    foreach ($f in $ExcludeFiles) { $excludeArg += " /xf `"$f`"" }
    $excludeArg += " /xd `"onlyoffice_plugins_repo`""

    $robocopyCmd = "robocopy `"$ProjectRoot`" `"$ProjectStage`" /E /COPY:DAT $excludeArg /NP /NFL /NDL /NJH /NJS"
    cmd /c $robocopyCmd 2>&1 | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "Robocopy failed with code $LASTEXITCODE" }

    # [2/3] Export Docker volumes
    Write-Host "`n[2/3] Exporting OnlyOffice Docker volumes..." -ForegroundColor Yellow
    foreach ($vol in $Volumes) {
        $exists = docker volume inspect $vol 2>$null
        if ($LASTEXITCODE -eq 0) {
            Write-Host "   Exporting $vol ..."
            docker run --rm -v ${vol}:/src -v "${VolStage}:/out" alpine sh -c "tar czf /out/${vol}.tar.gz -C /src ." 2>&1 | Out-Null
            if ($LASTEXITCODE -eq 0) { Write-Host "   OK: $vol" }
            else                     { Write-Host "   WARN: $vol export failed" -ForegroundColor Yellow }
        } else {
            Write-Host "   SKIP: $vol (not found)" -ForegroundColor Gray
        }
    }

    # Write metadata
    @"
HUSKY TRANSLATE Backup
======================
Date       : $Date
Project    : $ProjectRoot
Services   : husky_portal (nginx:alpine, :8070) + husky_onlyoffice (documentserver, :8090)
Architecture: Frontend portal + OnlyOffice Document Server + AI/Translation plugins
Restore    : Run ts_restore_centos.sh on target Linux server
"@ | Set-Content -Path $MetadataFile -Encoding UTF8

    # [3/3] Package to 7z
    Write-Host "`n[3/3] Creating 7z archive..." -ForegroundColor Yellow
    Invoke-Cmd "`"$ZipExe`" a -t7z -mx5 -mmt=on `"$ArchiveFile`" `"$StageRoot\*`"" "Pack to 7z"

    $size = [math]::Round((Get-Item $ArchiveFile).Length / 1MB, 1)
    Write-Host ""
    Write-Host "=== BACKUP COMPLETE ===" -ForegroundColor Green
    Write-Host "File : $ArchiveFile"
    Write-Host "Size : $size MB"
    Write-Host ""

    # Purge old backups
    if ($RetentionDays -gt 0) {
        Get-ChildItem $BackupRoot -Filter "husky_trans_backup_*.7z" |
            Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-$RetentionDays) } |
            ForEach-Object { Remove-Item $_.FullName; Write-Host "Purged: $($_.Name)" }
    }

} finally {
    if (Test-Path $StageRoot) {
        Remove-Item -LiteralPath $StageRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
