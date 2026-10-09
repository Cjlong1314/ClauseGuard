# ClauseGuard一键部署/升级/回滚（Windows PowerShell版，功能同deploy.sh）
# 用法: powershell -File deploy.ps1 install|upgrade|rollback|stop|start|status
param([string]$Action = "status")
$ErrorActionPreference = "Stop"
$deployDir = Split-Path -Parent $PSScriptRoot
Set-Location $deployDir

switch ($Action) {
  "install" {
    if (-not (Test-Path "$deployDir\.env")) {
      Copy-Item "$deployDir\.env.example" "$deployDir\.env"
      Write-Host "已生成.env，请按需修改后重新执行install"
      return
    }
    docker compose up -d db
    Write-Host "等待数据库就绪..."
    do { Start-Sleep 2 } while (-not (docker exec clauseguard-db pg_isready -U clauseguard 2>$null))
    docker compose up -d app
    Write-Host "部署完成，健康检查:"
    powershell -File "$PSScriptRoot\health_check.ps1"
  }
  "upgrade" {
    Write-Host "== 升级前备份 =="
    powershell -File "$PSScriptRoot\backup.ps1" -Action backup
    docker compose build app
    docker compose up -d app
    Write-Host "升级完成，健康检查:"
    powershell -File "$PSScriptRoot\health_check.ps1"
  }
  "rollback" {
    $tags = docker images clauseguard-app --format "{{.Tag}}"
    $prev = $tags | Select-Object -Index 1
    if (-not $prev) { Write-Host "无可回滚的旧镜像"; return }
    docker tag "clauseguard-app:$prev" clauseguard-app:current
    docker compose up -d app
    Write-Host "已回滚到 $prev"
  }
  "stop"   { docker compose stop }
  "start"  { docker compose start }
  "status" { docker compose ps; powershell -File "$PSScriptRoot\health_check.ps1" }
  default  { Write-Host "用法: deploy.ps1 install|upgrade|rollback|stop|start|status" }
}
