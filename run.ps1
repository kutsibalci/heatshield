# One-click demo: starts the simulator + API (NAC_MODE=simulator). For fixture mode: .\run.ps1 -Mode fixture
param([string]$Mode = "simulator", [int]$ApiPort = 8000, [int]$SimPort = 8081)
$py = "python"
$env:NAC_MODE = $Mode
if (-not $env:HS_DEMO_MODE) { $env:HS_DEMO_MODE = "1" }   # local demo: the six scenarios are enabled (rejected in live mode)
$env:NAC_FIXTURE_PATH = "$PSScriptRoot\fixtures\profiles.json"
if ($Mode -eq "simulator") {
  $env:NAC_BASE_URL = "http://127.0.0.1:$SimPort"
  Start-Process -NoNewWindow $py -ArgumentList "-m","uvicorn","apps.simulator.main:app","--port","$SimPort" -WorkingDirectory $PSScriptRoot
  Start-Sleep -Seconds 1
}
Write-Host "API: http://127.0.0.1:$ApiPort  (demo: http://127.0.0.1:$ApiPort/demo)  mode=$Mode"
& $py -m uvicorn apps.api.main:app --port $ApiPort --reload
