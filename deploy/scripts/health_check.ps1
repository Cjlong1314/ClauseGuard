# ClauseGuard健康检查三探针（Windows PowerShell版，功能同health_check.sh）
# 用法: powershell -File health_check.ps1 [-AppUrl http://127.0.0.1:8600]
# 退出码: 0健康; 1降级可用; 2不健康
param([string]$AppUrl = "http://127.0.0.1:8600")
$ErrorActionPreference = "SilentlyContinue"
$rc = 2
Write-Host "== ClauseGuard健康检查 $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') =="

# 探针1: 应用/health（聚合db/llm/embedding，降级可报）
try {
  $body = Invoke-RestMethod -Uri "$AppUrl/health" -TimeoutSec 8
  Write-Host ("[应用] /health整体: {0}" -f $body.status)
  foreach ($c in $body.checks) {
    Write-Host ("[{0}] {1} {2}" -f $c.component.ToUpper(), $c.status, $c.message)
  }
  $db = $body.checks | Where-Object { $_.component -eq "database" }
  # 与/health整体状态一致：全ok=0；db ok但其他degraded=1（降级可用）；db挂=2
  if ($body.status -eq "ok") { $rc = 0 }
  elseif ($db.status -eq "ok") { $rc = 1 }
  else { $rc = 2 }
} catch {
  Write-Host "[应用] DOWN 无法连接 $AppUrl/health"
  Write-Host "结论: 不健康"
  exit 2
}

# 探针2: 数据库TCP直连兜底（无需psql客户端）
$dbHost = "127.0.0.1"; $dbPort = 5432
if ($env:DATABASE_URL -match "://([^:/@]+):[^@/]+@([^:/]+):(\d+)") { $dbHost = $Matches[2]; $dbPort = [int]$Matches[3] }
try {
  $tcp = New-Object Net.Sockets.TcpClient
  $tcp.Connect($dbHost, $dbPort) | Out-Null
  $tcp.Close()
  Write-Host "[数据库] TCP $dbHost`:$dbPort 可达"
} catch {
  Write-Host "[数据库] TCP $dbHost`:$dbPort 不可达"; $rc = 2
}

# 探针3: LLM服务（未配置视为降级）
$llm = ($body.checks | Where-Object { $_.component -eq "llm" })
if ($llm.configured) {
  try {
    $u = $llm.base_url.TrimEnd("/") + "/models"
    Invoke-RestMethod -Uri $u -TimeoutSec 8 | Out-Null
    Write-Host ("[LLM服务] {0} 可达" -f $llm.base_url)
  } catch {
    # 404/401说明服务在线但端点受限，同样视为可达
    $code = 0
    try { $code = [int]$_.Exception.Response.StatusCode } catch {}
    if ($code -ge 400 -and $code -lt 500) {
      Write-Host ("[LLM服务] {0} 在线（/models受限，HTTP {1}）" -f $llm.base_url, $code)
    } else {
      Write-Host ("[LLM服务] {0} 不可达（AI研判降级为规则引擎）" -f $llm.base_url)
      if ($rc -eq 0) { $rc = 1 }
    }
  }
} else {
  Write-Host "[LLM服务] 未配置（degraded，规则引擎基线兜底）"
  if ($rc -eq 0) { $rc = 1 }
}

switch ($rc) { 0 { Write-Host "结论: 健康" } 1 { Write-Host "结论: 降级可用" } 2 { Write-Host "结论: 不健康" } }
exit $rc
