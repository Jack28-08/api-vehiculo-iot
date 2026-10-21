#requires -Version 5.1
<#
.SYNOPSIS
Instala o actualiza el servicio API solamente dentro de EC2 Windows Server.
.DESCRIPTION
No imprime .env, modifica RDS, genera claves ni cambia firewall. Detener primero
Uvicorn manual con Ctrl+C. La validacion local del parser no prueba el servicio.
#>
[CmdletBinding()]
param(
    [string]$ProjectRoot = '',
    [string]$ServiceDirectory = 'C:\IoT\servicios\api',
    [switch]$InstallOnThisServer
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Windows-ServiceCommon.ps1')
Assert-ServerTarget -InstallOnThisServer:$InstallOnThisServer
if (-not $ProjectRoot) { $ProjectRoot = Join-Path $PSScriptRoot '..\..' }

$project = (Resolve-Path -LiteralPath $ProjectRoot).Path.TrimEnd('\')
$python = Join-Path $project '.venv\Scripts\python.exe'
$envFile = Join-Path $project '.env'
foreach ($required in @($python, $envFile, (Join-Path $project 'app\main.py'))) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) { throw "Falta un archivo requerido: $required" }
}
$pythonBase = (& $python -c 'import sys; print(sys.base_prefix)' | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $pythonBase -PathType Container)) {
    throw 'No se pudo determinar el Python base del entorno virtual.'
}
$helpText = & $python -m uvicorn --help 2>&1
if ($LASTEXITCODE -ne 0 -or ($helpText -join ' ') -notmatch 'timeout-graceful-shutdown') {
    throw 'Falta el Uvicorn fijado en requirements.txt.'
}
$serviceId = 'ApiVehiculoIoT'
$servicePath = Get-SafeServicePath -Path $ServiceDirectory -ProjectRoot $project
$wrapper = Join-Path $servicePath 'api-iot.exe'
$configPath = Join-Path $servicePath 'api-iot.xml'
$logPath = Join-Path $servicePath 'logs'
$existing = Get-CimInstance Win32_Service -Filter "Name='$serviceId'"
Assert-ServiceIdentity -Service $existing -Wrapper $wrapper
if ($null -ne $existing) {
    $runningDependents = @((Get-Service -Name $serviceId).DependentServices | Where-Object { $_.Status -ne 'Stopped' })
    if ($runningDependents.Count -gt 0) {
        throw 'Detenga primero CaddyVehiculoIoT (u otros dependientes identificados) antes de actualizar la API. No se usa Force.'
    }
}
if (@(Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue).Count -gt 0 -and
    ($null -eq $existing -or $existing.State -ne 'Running')) {
    throw 'Puerto 8000 ocupado. Detenga Uvicorn manual con Ctrl+C; no se termina ningun proceso.'
}

New-Item -ItemType Directory -Path $servicePath, $logPath -Force | Out-Null
Set-RestrictedDirectoryAcl -Directory $servicePath
Set-RestrictedDirectoryAcl -Directory $logPath -LocalServiceRights 'Modify'
Get-WinSW -Destination $wrapper
$xml = New-ApiServiceXml -Python $python -Project $project -LogPath $logPath
Stop-ManagedService -Service $existing
if (@(Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue).Count -gt 0) {
    throw 'Puerto 8000 sigue ocupado. Se conserva el servicio detenido; no se mata un proceso ajeno.'
}
# LocalService receives RX only on code/Python, not all of Administrator's profile.
Grant-CheckedAccess -Target $project -Rule '*S-1-5-19:(OI)(CI)RX'
Grant-CheckedAccess -Target $pythonBase -Rule '*S-1-5-19:(OI)(CI)RX'
Grant-ParentTraversal -Directory $pythonBase
Grant-ParentTraversal -Directory $project
Grant-ParentTraversal -Directory $servicePath
Set-PrivateEnvAcl -EnvFile $envFile -BackupDirectory $servicePath
Write-ServiceConfig -Path $configPath -Xml $xml
Start-ManagedService -Id $serviceId -Wrapper $wrapper -AlreadyInstalled ($null -ne $existing)

$ready = $false
$deadline = [DateTime]::UtcNow.AddSeconds(60)
while ([DateTime]::UtcNow -lt $deadline) {
    try {
        $health = Invoke-RestMethod -Uri 'http://127.0.0.1:8000/health' -TimeoutSec 5
        $dbHealth = Invoke-RestMethod -Uri 'http://127.0.0.1:8000/health/db' -TimeoutSec 12
        if ($health.status -eq 'ok' -and $dbHealth.status -eq 'ok') { $ready = $true; break }
    } catch { }
    Start-Sleep -Seconds 2
}
if (-not $ready) { throw "No se comprobaron ambos health. Revise logs locales en $logPath sin compartir secretos." }
$listeners = @(Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction Stop)
if ($listeners.Count -eq 0 -or @($listeners | Where-Object { $_.LocalAddress -ne '127.0.0.1' }).Count -gt 0) {
    throw 'No se confirmo escucha exclusivamente en 127.0.0.1:8000.'
}
Get-CimInstance Win32_Service -Filter "Name='$serviceId'" | Select-Object Name, State, StartMode, StartName, ProcessId
Write-Host 'OK: API loopback, un worker, /health y /health/db. Comprobar persistencia tras salir de RDP y reiniciar EC2.'
