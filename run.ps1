# Tek tuşla demo: simülatör + API'yi başlatır (NAC_MODE=simulator). Fixture modu için: .\run.ps1 -Mode fixture
param([string]$Mode = "simulator", [int]$ApiPort = 8000, [int]$SimPort = 8081)
$py = "python"
if (Test-Path "C:\Users\Acer\AppData\Local\Programs\Python\Python313\python.exe") { $py = "C:\Users\Acer\AppData\Local\Programs\Python\Python313\python.exe" }
$env:NAC_MODE = $Mode
$env:NAC_FIXTURE_PATH = "$PSScriptRoot\fixtures\profiles.json"
if ($Mode -eq "simulator") {
  $env:NAC_BASE_URL = "http://127.0.0.1:$SimPort"
  Start-Process -NoNewWindow $py -ArgumentList "-m","uvicorn","apps.simulator.main:app","--port","$SimPort" -WorkingDirectory $PSScriptRoot
  Start-Sleep -Seconds 1
}
Write-Host "API: http://127.0.0.1:$ApiPort  (demo: http://127.0.0.1:$ApiPort/demo)  mode=$Mode"
& $py -m uvicorn apps.api.main:app --port $ApiPort --reload
