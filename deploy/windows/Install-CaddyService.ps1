#requires -Version 5.1
<#
.SYNOPSIS
Configura Caddy HTTPS/WSS como servicio en EC2 Windows Server.
.DESCRIPTION
Requiere DNS publico y API ya instalada. Sin TLS inseguro ni certificados locales.
-OpenFirewall agrega solamente dos reglas TCP 80/443 para caddy.exe, sin tocar
RDP, MySQL, Security Groups ni reglas existentes de otros programas.
#>
[CmdletBinding()]
param(
    [string]$Hostname = '',
    [string]$ExpectedPublicIPv4 = '184.72.72.255',
    [string]$ProjectRoot = '',
    [string]$ServiceDirectory = 'C:\IoT\servicios\caddy',
    [switch]$OpenFirewall,
    [switch]$InstallOnThisServer
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Windows-ServiceCommon.ps1')
Assert-ServerTarget -InstallOnThisServer:$InstallOnThisServer
if (-not $ProjectRoot) { $ProjectRoot = Join-Path $PSScriptRoot '..\..' }
if ($Hostname -match '(?i)(?:^localhost$|\.(?:localhost|local|internal|invalid|test|example)$|\.home\.arpa$)') {
    throw 'Use un hostname DNS publico; no se emiten certificados locales o de prueba.'
}

$project = (Resolve-Path -LiteralPath $ProjectRoot).Path.TrimEnd('\')
$servicePath = Get-SafeServicePath -Path $ServiceDirectory -ProjectRoot $project
$dataPath = Join-Path $servicePath 'data'
$logPath = Join-Path $servicePath 'logs'
$configPath = Join-Path $servicePath 'Caddyfile'
$executable = Join-Path $servicePath 'caddy.exe'
$wrapper = Join-Path $servicePath 'caddy-service.exe'
$xmlPath = Join-Path $servicePath 'caddy-service.xml'
$serviceId = 'CaddyVehiculoIoT'
$config = New-CaddyConfig -Hostname $Hostname -DataPath $dataPath -AccessLog (Join-Path $logPath 'access.json')
$ipv4 = $null
if (-not [Net.IPAddress]::TryParse($ExpectedPublicIPv4, [ref]$ipv4) -or $ipv4.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork) {
    throw 'ExpectedPublicIPv4 debe ser la IPv4 elastica publica de esta EC2.'
}
$answers = @(Resolve-DnsName -Name $Hostname -Type A -DnsOnly -ErrorAction Stop | Where-Object { $_.Type -eq 'A' })
if ($answers.Count -eq 0 -or @($answers | Where-Object { $_.IPAddress -ne $ExpectedPublicIPv4 }).Count -gt 0) {
    throw 'DNS A no coincide exclusivamente con ExpectedPublicIPv4. Corrija DNS antes de solicitar un certificado.'
}
$ipv6Answers = @(Resolve-DnsName -Name $Hostname -Type AAAA -DnsOnly -ErrorAction SilentlyContinue | Where-Object { $_.Type -eq 'AAAA' })
if ($ipv6Answers.Count -gt 0) { throw 'Este despliegue es IPv4. Retire AAAA o configure/verifique IPv6 antes de continuar.' }
$api = Get-CimInstance Win32_Service -Filter "Name='ApiVehiculoIoT'"
if ($null -eq $api -or $api.State -ne 'Running') { throw 'Instale y compruebe primero ApiVehiculoIoT.' }
$health = Invoke-RestMethod -Uri 'http://127.0.0.1:8000/health' -TimeoutSec 5
if ($health.status -ne 'ok') { throw 'La API local no esta lista.' }
$existing = Get-CimInstance Win32_Service -Filter "Name='$serviceId'"
Assert-ServiceIdentity -Service $existing -Wrapper $wrapper
if (@(Get-NetTCPConnection -LocalPort 80,443 -State Listen -ErrorAction SilentlyContinue).Count -gt 0 -and
    ($null -eq $existing -or $existing.State -ne 'Running')) {
    throw '80/443 ocupado por otro proceso. No se detiene ni desinstala ningun servicio ajeno.'
}

New-Item -ItemType Directory -Path $servicePath, $dataPath, $logPath -Force | Out-Null
Set-RestrictedDirectoryAcl -Directory $servicePath
Set-RestrictedDirectoryAcl -Directory $dataPath -LocalServiceRights 'Modify'
Set-RestrictedDirectoryAcl -Directory $logPath -LocalServiceRights 'Modify'
Grant-ParentTraversal -Directory $servicePath
Get-WinSW -Destination $wrapper

# Version fixed, not /latest at install time. Both archive and executable are pinned.
$caddyVersion = '2.11.7'
$archiveHash = '0A1EDC0B799512051C57071CE0E798D3F2CF67DC98171366F1D2B326072E3B06'
$binaryHash = '5F93A9BD555F3AAC9D33AF2756BA14B3519D8CCA4FFD55256BDAEAB444C9DB2A'
if (Test-Path -LiteralPath $executable -PathType Leaf) {
    if ((Get-FileHash -LiteralPath $executable -Algorithm SHA256).Hash -ne $binaryHash) {
        throw 'caddy.exe existente tiene otro hash. Se conserva sin ejecutar; revise la version antes de actualizar.'
    }
} else {
    $archive = Join-Path $servicePath ('caddy_' + $caddyVersion + '_windows_amd64.zip')
    Get-PinnedFile -Url "https://github.com/caddyserver/caddy/releases/download/v$caddyVersion/caddy_${caddyVersion}_windows_amd64.zip" -Destination $archive -Sha256 $archiveHash
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [IO.Compression.ZipFile]::OpenRead($archive)
    $extracted = $executable + '.' + [guid]::NewGuid().ToString('N') + '.download'
    try {
        $entry = $zip.GetEntry('caddy.exe')
        if ($null -eq $entry) { throw 'El ZIP oficial no contiene caddy.exe.' }
        [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $extracted, $false)
        if ((Get-FileHash -LiteralPath $extracted -Algorithm SHA256).Hash -ne $binaryHash) {
            throw 'Hash de caddy.exe extraido incorrecto. No se ejecuta.'
        }
        Move-Item -LiteralPath $extracted -Destination $executable
    } finally {
        $zip.Dispose()
        if (Test-Path -LiteralPath $extracted -PathType Leaf) { Remove-Item -LiteralPath $extracted -Force }
    }
}

$candidate = Join-Path $servicePath ('Caddyfile.' + [guid]::NewGuid().ToString('N') + '.candidate')
try {
    [IO.File]::WriteAllText($candidate, $config, (New-Object Text.UTF8Encoding($false)))
    # Caddy emits INFO on stderr even on success; inspect native exit code on PS5.1.
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $executable validate --config $candidate --adapter caddyfile
        $validationCode = $LASTEXITCODE
    } finally { $ErrorActionPreference = $previousPreference }
    if ($validationCode -ne 0) { throw 'Caddy rechaza la configuracion. Servicio anterior conservado.' }
    Stop-ManagedService -Service $existing -Timeout 45
    if (@(Get-NetTCPConnection -LocalPort 80,443 -State Listen -ErrorAction SilentlyContinue).Count -gt 0) {
        throw '80/443 sigue ocupado. Servicio detenido; no se mata otro proceso.'
    }
    if (Test-Path -LiteralPath $configPath -PathType Leaf) {
        Copy-Item -LiteralPath $configPath -Destination ($configPath + '.' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff') + '.bak')
    }
    Move-Item -LiteralPath $candidate -Destination $configPath -Force
} finally {
    if (Test-Path -LiteralPath $candidate -PathType Leaf) { Remove-Item -LiteralPath $candidate -Force }
}
$xml = New-CaddyServiceXml -Executable $executable -ConfigPath $configPath -WorkingDirectory $servicePath -LogPath $logPath
Write-ServiceConfig -Path $xmlPath -Xml $xml

if ($OpenFirewall) {
    foreach ($port in @(80,443)) {
        $ruleName = 'CaddyVehiculoIoT-TCP-' + $port
        $rule = Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue
        if ($null -eq $rule) {
            New-NetFirewallRule -Name $ruleName -DisplayName $ruleName -Direction Inbound -Action Allow -Protocol TCP -LocalPort $port -Program $executable -Profile Any | Out-Null
        } else {
            $app = $rule | Get-NetFirewallApplicationFilter
            $ports = $rule | Get-NetFirewallPortFilter
            if ($app.Program -ne $executable -or $ports.LocalPort -ne [string]$port -or $ports.Protocol -ne 'TCP' -or
                $rule.Direction -ne 'Inbound' -or $rule.Action -ne 'Allow' -or $rule.Enabled -ne 'True') {
                throw 'Regla de firewall con mismo nombre y otra configuracion. Se conserva; revise manualmente.'
            }
        }
    }
}
Start-ManagedService -Id $serviceId -Wrapper $wrapper -AlreadyInstalled ($null -ne $existing)
Get-CimInstance Win32_Service -Filter "Name='$serviceId'" | Select-Object Name, State, StartMode, StartName, ProcessId
Write-Host 'Servicio iniciado. La emision ACME es asincrona y AUN debe comprobarse desde fuera de EC2.'
Write-Host ('Comprobar certificado valido, HTTPS /health y /docs, HTTP->HTTPS y WSS en ' + $Hostname)
Write-Host 'No use -SkipCertificateCheck. Tras verificar el proxy, retire solo la regla publica 8000 del SG de EC2.'
