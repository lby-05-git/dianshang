$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectsRoot = Split-Path -Parent $ProjectRoot
$ShopRoot = Get-ChildItem -LiteralPath $ProjectsRoot -Directory -Filter "eget*" |
    Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName "server.py") } |
    Select-Object -First 1 -ExpandProperty FullName
if (-not $ShopRoot) {
    throw "Cannot find an eget* sibling directory containing server.py."
}
$VenvRoot = Join-Path $ProjectRoot ".venv"
$VenvPython = Join-Path $VenvRoot "Scripts\python.exe"
$PythonExe = $VenvPython
$VenvConfig = Join-Path $VenvRoot "pyvenv.cfg"
if (Test-Path -LiteralPath $VenvConfig) {
    $PythonHomeLine = Get-Content -LiteralPath $VenvConfig |
        Where-Object { $_ -match '^home\s*=\s*' } | Select-Object -First 1
    if ($PythonHomeLine) {
        $BasePython = Join-Path (($PythonHomeLine -replace '^home\s*=\s*', '').Trim()) "python.exe"
        if (Test-Path -LiteralPath $BasePython) {
            # Some restricted sessions cannot execute the venv launcher directly.
            # Use the base interpreter declared by pyvenv.cfg with project dependencies.
            $PythonExe = $BasePython
            $env:VIRTUAL_ENV = $VenvRoot
            $env:PYTHONPATH = Join-Path $VenvRoot "Lib\site-packages"
        }
    }
}
if ($env:EGET_PYTHON_EXE) {
    if (-not (Test-Path -LiteralPath $env:EGET_PYTHON_EXE)) {
        throw "EGET_PYTHON_EXE does not exist: $env:EGET_PYTHON_EXE"
    }
    # Explicit override for restricted sessions; normal launches keep the project venv.
    $PythonExe = $env:EGET_PYTHON_EXE
    $env:PYTHONPATH = $null
    $env:VIRTUAL_ENV = Split-Path -Parent (Split-Path -Parent $PythonExe)
}
$ShopServer = Join-Path $ShopRoot "server.py"

function Get-ListeningProcessIds([int]$Port) {
    $connections = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    $ids = @($connections | Select-Object -ExpandProperty OwningProcess -Unique)
    if ($ids.Count -gt 0) {
        return $ids
    }
    # Fall back to netstat when Get-NetTCPConnection is unavailable.
    # Match only LISTENING sockets on the requested local port.
    $pattern = "^\s*TCP\s+\S+:$Port\s+\S+\s+LISTENING\s+(\d+)\s*$"
    return @(netstat -ano -p tcp | ForEach-Object {
        if ($_ -match $pattern) { [int]$Matches[1] }
    } | Sort-Object -Unique)
}

Write-Host "[2/4] Waiting for backend health check..."
$backendReady = $false
foreach ($attempt in 1..45) {
    try {
        $health = Invoke-RestMethod "http://127.0.0.1:8001/api/v1/health" -TimeoutSec 2
        if ($health.status -eq "ok") {
            $backendReady = $true
            break
        }
    }
    catch {
        Start-Sleep -Seconds 1
    }
}
if (-not $backendReady) {
    throw "Backend did not become healthy on port 8001."
}

# Health alone does not prove that the rebuilt image loaded multi-evidence routes.
# Require both supervisor evidence APIs before continuing.
$backendEvidenceReady = $false
try {
    $openApi = Invoke-RestMethod "http://127.0.0.1:8001/api/openapi.json" -TimeoutSec 5
    $routeNames = @($openApi.paths.PSObject.Properties.Name)
    $backendEvidenceReady = (
        $routeNames -contains "/api/v1/refunds/{case_id}/evidences" -and
        $routeNames -contains "/api/v1/refunds/{case_id}/evidences/{evidence_id}"
    )
}
catch {}
if (-not $backendEvidenceReady) {
    throw "Backend is healthy but multi-attachment evidence APIs are not loaded. Rebuild backend and worker images."
}

Write-Host "[3/4] Starting eget shopping site on port 8002..."
if (-not (Test-Path -LiteralPath $PythonExe)) {
    throw "Project virtual environment not found: $PythonExe"
}
if (-not (Test-Path -LiteralPath $ShopServer)) {
    throw "Shopping server not found: $ShopServer"
}
$shopProcessIds = @(Get-ListeningProcessIds 8002)
if ($shopProcessIds.Count -gt 0) {
    # Verify that the listener is eget before replacing an outdated process.
    $hasCurrentShopFeatures = $false
    $isEgetShop = $false
    try {
        $shopHealth = Invoke-RestMethod "http://127.0.0.1:8002/api/health" -TimeoutSec 2
        $isEgetShop = $shopHealth.eget -eq $true
        $hasCurrentShopFeatures = (
            $isEgetShop -and
            $shopHealth.knowledge_assistant -eq $true -and
            $shopHealth.order_voucher -eq $true -and
            [int]$shopHealth.order_voucher_version -ge 2
        )
    }
    catch {}
    if (-not $hasCurrentShopFeatures) {
        # A closing legacy process can keep port 8002 briefly. Recheck for up to 10 seconds.
        if (-not $isEgetShop) {
            Write-Host "Port 8002 did not answer as eget; waiting for a transient listener to exit..."
            foreach ($attempt in 1..20) {
                Start-Sleep -Milliseconds 500
                $shopProcessIds = @(Get-ListeningProcessIds 8002)
                if ($shopProcessIds.Count -eq 0) {
                    break
                }
                try {
                    $shopHealth = Invoke-RestMethod "http://127.0.0.1:8002/api/health" -TimeoutSec 2
                    $isEgetShop = $shopHealth.eget -eq $true
                    $hasCurrentShopFeatures = (
                        $isEgetShop -and
                        $shopHealth.knowledge_assistant -eq $true -and
                        $shopHealth.order_voucher -eq $true -and
                        [int]$shopHealth.order_voucher_version -ge 2
                    )
                    if ($isEgetShop) { break }
                }
                catch {}
            }
        }
        if ($hasCurrentShopFeatures) {
            Write-Host "Existing shopping site already has the current feature contract."
        }
        elseif ($shopProcessIds.Count -eq 0) {
            Write-Host "Transient listener released port 8002; starting the shopping site..."
        }
        else {
            # Stop a legacy process only when its command line points to this eget server.py.
            $ownedShopPids = @()
            foreach ($processId in $shopProcessIds) {
                $candidate = Get-CimInstance Win32_Process -Filter "ProcessId=$processId" -ErrorAction SilentlyContinue
                if ($candidate) {
                    $candidateCommand = [string]$candidate.CommandLine
                    $serverNeedle = $ShopServer.ToLowerInvariant()
                    if ($candidateCommand.ToLowerInvariant().Contains($serverNeedle)) {
                        $ownedShopPids += $processId
                    }
                }
            }
            if ($ownedShopPids.Count -eq $shopProcessIds.Count) {
                $isEgetShop = $true
            }
        }

        if (($hasCurrentShopFeatures -eq $false) -and ($shopProcessIds.Count -gt 0)) {
            if ($isEgetShop -eq $false) {
                $occupiedPidText = $shopProcessIds -join ", "
                throw ("Port 8002 is persistently occupied by another process. PID: " + $occupiedPidText)
            }
            Write-Host "Existing shopping process is outdated; restarting it to load current features..."
            foreach ($processId in $shopProcessIds) {
                Stop-Process -Id $processId -Force
            }
            Start-Sleep -Milliseconds 500
            $shopProcessIds = @(Get-ListeningProcessIds 8002)
        }
    }
}
if ($shopProcessIds.Count -eq 0) {
    # server.py resolves web/data paths from __file__, so an absolute script path
    # avoids Start-Process rejecting a Unicode/apostrophe-containing working path.
    Start-Process -FilePath $PythonExe -ArgumentList @($ShopServer) `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $ShopRoot "server.stdout.log") `
        -RedirectStandardError (Join-Path $ShopRoot "server.stderr.log")
}

# Require the voucher-v2 health contract, not just an open port.
$shopFeatureReady = $false
foreach ($attempt in 1..20) {
    try {
        $shopHealth = Invoke-RestMethod "http://127.0.0.1:8002/api/health" -TimeoutSec 2
        if ($shopHealth.order_voucher -eq $true -and
            [int]$shopHealth.order_voucher_version -ge 2) {
            $shopFeatureReady = $true
            break
        }
    }
    catch {}
    Start-Sleep -Milliseconds 300
}
if (-not $shopFeatureReady) {
    throw "Shopping site is running but attachment upload feature v2 is not loaded."
}

Write-Host "[4/4] Starting product frontend on port 5173..."
$frontListening = Get-NetTCPConnection -LocalPort 5173 -State Listen -ErrorAction SilentlyContinue
if (-not $frontListening) {
    $npm = (Get-Command npm.cmd -ErrorAction Stop).Source
    Start-Process -FilePath $npm -ArgumentList @("run", "dev") `
        -WorkingDirectory (Join-Path $ProjectRoot "frontend") -WindowStyle Hidden
}

Write-Host ""
Write-Host "All services started."
Write-Host "Product console: http://127.0.0.1:5173/#/"
Write-Host "Shopping site : http://127.0.0.1:8002/"
Write-Host "Backend API   : http://127.0.0.1:8001/api/docs"
Write-Host "Agent worker  : rfs-worker (Docker Compose)"
