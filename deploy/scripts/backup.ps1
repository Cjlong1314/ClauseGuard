# ClauseGuard备份/恢复脚本（Windows PowerShell版，功能同backup.sh/restore.sh）
# 每日计划任务（管理员PowerShell执行一次即可）:
#   schtasks /create /tn "ClauseGuard备份" /tr "powershell -File D:\ClauseGuard\deploy\scripts\backup.ps1" /sc daily /st 02:30
#
# WAL归档说明（Windows原生PG）: postgresql.conf中
#   archive_mode=on
#   archive_command='copy "%p" "D:\\pg_wal_archive\\%f"'
# 恢复时结合全量备份+wal归档重放到任意时间点（见deploy/README.md）
param(
  [string]$Action = "backup",          # backup | restore
  [string]$DumpFile = ""               # restore时必填
)
$ErrorActionPreference = "Stop"
$deployDir = Split-Path -Parent $PSScriptRoot
if (Test-Path "$deployDir\.env") {
  Get-Content "$deployDir\.env" | ForEach-Object {
    if ($_ -match "^\s*([A-Z_]+)=(.*)\s*$") { [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], "Process") }
  }
}
$backupDir = if ($env:BACKUP_DIR) { $env:BACKUP_DIR } else { "$deployDir\backups" }
$keepDays = if ($env:BACKUP_KEEP_DAYS) { [int]$env:BACKUP_KEEP_DAYS } else { 14 }
$user = if ($env:POSTGRES_USER) { $env:POSTGRES_USER } else { "clauseguard" }
$db = if ($env:POSTGRES_DB) { $env:POSTGRES_DB } else { "clauseguard" }
$dbUrl = if ($env:DATABASE_URL) { $env:DATABASE_URL } else { "postgresql://clauseguard:Cg%402026pg@127.0.0.1:5432/clauseguard" }

function Get-PgDumpCmd {
  # 优先容器内pg_dump，否则宿主机PATH中的pg_dump
  $cid = docker ps --format "{{.Names}}" | Select-String "^clauseguard-db$"
  if ($cid) { return { param($args) docker exec clauseguard-db pg_dump -U $user -d $db -Fc @args } }
  return { param($args) pg_dump --dbname=$dbUrl -Fc @args }
}

switch ($Action) {
  "backup" {
    if (-not (Test-Path $backupDir)) { New-Item -ItemType Directory -Path $backupDir | Out-Null }
    $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $out = "$backupDir\cg_full_$stamp.dump"
    Write-Host "== 全量备份 -> $out =="
    $cid = docker ps --format "{{.Names}}" | Select-String "^clauseguard-db$"
    if ($cid) {
      docker exec clauseguard-db pg_dump -U $user -d $db -Fc > $out
    } else {
      pg_dump --dbname=$dbUrl -Fc -f $out
    }
    Write-Host "备份完成: $((Get-Item $out).Length / 1MB -as [int]) MB"
    Get-ChildItem $backupDir -Filter "cg_full_*.dump" | Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-$keepDays) } | Remove-Item -Force
    Write-Host "已清理${keepDays}天前备份"
  }
  "restore" {
    if (-not $DumpFile -or -not (Test-Path $DumpFile)) { throw "用法: backup.ps1 -Action restore -DumpFile <备份文件>" }
    Write-Host "警告: 将覆盖数据库 $db，Ctrl+C可取消（10秒后继续）"
    Start-Sleep 10
    $cid = docker ps --format "{{.Names}}" | Select-String "^clauseguard-db$"
    if ($cid) {
      docker stop clauseguard-app | Out-Null
      docker exec clauseguard-db psql -U $user -d postgres -c "DROP DATABASE IF EXISTS `"$db`";"
      docker exec clauseguard-db psql -U $user -d postgres -c "CREATE DATABASE `"$db`";"
      Get-Content $DumpFile -Raw -Encoding Byte | docker exec -i clauseguard-db pg_restore -U $user -d $db
      docker start clauseguard-app | Out-Null
    } else {
      $plain = $dbUrl -replace "/clauseguard`?", "/postgres?"
      psql --dbname=$plain -c "DROP DATABASE IF EXISTS clauseguard;" -c "CREATE DATABASE clauseguard;"
      pg_restore --dbname=$dbUrl $DumpFile
    }
    Write-Host "恢复完成，请运行健康检查: powershell -File health_check.ps1"
  }
  default { throw "Action仅支持backup|restore" }
}
