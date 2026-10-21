#requires -Version 5.1
# Functions only: importing this file does not install or modify anything.
Set-StrictMode -Version Latest

function Assert-ServerTarget {
    param([switch]$InstallOnThisServer)
    if (-not $InstallOnThisServer) {
        throw 'Sin cambios. Ejecute dentro de EC2 con -InstallOnThisServer, no en la PC de desarrollo.'
    }
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
        throw 'Se requiere Windows Server y PowerShell elevado.'
    }
    $os = Get-CimInstance Win32_OperatingSystem
    if ($os.ProductType -eq 1) { throw 'Windows de escritorio rechazado. No se han cambiado servicios, ACL ni firewall.' }
    $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Abra PowerShell como administrador dentro de EC2.'
    }
}

function ConvertTo-XmlText([string]$Value) { return [Security.SecurityElement]::Escape($Value) }

function Get-SafeServicePath {
    param([string]$Path, [string]$ProjectRoot = '')
    if (-not [IO.Path]::IsPathRooted($Path) -or $Path.StartsWith('\\')) {
        throw 'ServiceDirectory debe ser una ruta absoluta en disco local.'
    }
    $full = [IO.Path]::GetFullPath($Path).TrimEnd('\')
    $drive = [IO.Path]::GetPathRoot($full).TrimEnd('\')
    if ($full -eq $drive -or $full.StartsWith($env:SystemRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'ServiceDirectory no puede ser la raiz del disco ni un directorio de Windows.'
    }
    if ($ProjectRoot -and ($full -eq $ProjectRoot -or $full.StartsWith($ProjectRoot + '\', [StringComparison]::OrdinalIgnoreCase) -or
        $ProjectRoot.StartsWith($full + '\', [StringComparison]::OrdinalIgnoreCase))) {
        throw 'Guarde binarios, logs y certificados fuera del repositorio.'
    }
    $cursor = $full
    while ($cursor) {
        if (Test-Path -LiteralPath $cursor) {
            $item = Get-Item -LiteralPath $cursor -Force
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw 'ServiceDirectory y sus ancestros no pueden ser junctions ni enlaces.'
            }
        }
        $parent = [IO.Directory]::GetParent($cursor)
        if ($null -eq $parent) { break }
        $cursor = $parent.FullName
    }
    return $full
}

function Assert-ServiceIdentity {
    param($Service, [string]$Wrapper)
    if ($null -eq $Service) { return }
    $match = [regex]::Match($Service.PathName.Trim(), '^(?:"(?<path>[^"]+)"|(?<path>\S+))(?:\s|$)')
    if (-not $match.Success -or [IO.Path]::GetFullPath($match.Groups['path'].Value) -ne $Wrapper) {
        throw 'Ya existe ese servicio con otro ejecutable. Se conserva sin modificar.'
    }
    try {
        $account = New-Object Security.Principal.NTAccount($Service.StartName)
        $sid = $account.Translate([Security.Principal.SecurityIdentifier]).Value
    } catch { throw 'No se pudo verificar la cuenta del servicio existente. Se conserva.' }
    if ($sid -ne 'S-1-5-19') { throw 'El servicio existente no usa LocalService. Se conserva sin modificar.' }
}

function Get-PinnedFile {
    param([string]$Url, [string]$Destination, [string]$Sha256)
    if (Test-Path -LiteralPath $Destination -PathType Leaf) {
        if ((Get-FileHash -LiteralPath $Destination -Algorithm SHA256).Hash -ne $Sha256) {
            throw 'Existe un binario con otro SHA-256. Se conserva y no se ejecuta; revise la version.'
        }
        return
    }
    $partial = $Destination + '.' + [guid]::NewGuid().ToString('N') + '.download'
    $previousTls = [Net.ServicePointManager]::SecurityProtocol
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $partial
        if ((Get-FileHash -LiteralPath $partial -Algorithm SHA256).Hash -ne $Sha256) {
            throw 'SHA-256 descargado incorrecto. No se ejecuta el archivo.'
        }
        Move-Item -LiteralPath $partial -Destination $Destination
    } finally {
        [Net.ServicePointManager]::SecurityProtocol = $previousTls
        if (Test-Path -LiteralPath $partial -PathType Leaf) { Remove-Item -LiteralPath $partial -Force }
    }
}

function Get-WinSW {
    param([string]$Destination)
    Get-PinnedFile -Url 'https://github.com/winsw/winsw/releases/download/v2.12.0/WinSW-x64.exe' `
        -Destination $Destination -Sha256 '05B82D46AD331CC16BDC00DE5C6332C1EF818DF8CEEFCD49C726553209B3A0DA'
}

function Grant-CheckedAccess {
    param([string]$Target, [string]$Rule)
    & "$env:SystemRoot\System32\icacls.exe" $Target /grant $Rule /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "No se pudieron ajustar permisos en $Target" }
}

function Grant-ParentTraversal {
    param([string]$Directory)
    $parent = [IO.Directory]::GetParent($Directory)
    while ($null -ne $parent -and $null -ne $parent.Parent) {
        # Traverse only, this directory only: no listing or inherited profile access.
        Grant-CheckedAccess -Target $parent.FullName -Rule '*S-1-5-19:(X)'
        $parent = $parent.Parent
    }
}

function Set-RestrictedDirectoryAcl {
    param([string]$Directory, [string]$LocalServiceRights = 'ReadAndExecute')
    $acl = Get-Acl -LiteralPath $Directory
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($rule in @($acl.Access)) { [void]$acl.RemoveAccessRuleSpecific($rule) }
    foreach ($entry in @(@('S-1-5-18','FullControl'), @('S-1-5-32-544','FullControl'), @('S-1-5-19',$LocalServiceRights))) {
        $sid = New-Object Security.Principal.SecurityIdentifier($entry[0])
        $rule = New-Object Security.AccessControl.FileSystemAccessRule($sid, $entry[1], 'ContainerInherit,ObjectInherit', 'None', 'Allow')
        [void]$acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $Directory -AclObject $acl
}

function Set-PrivateEnvAcl {
    param([string]$EnvFile, [string]$BackupDirectory)
    $acl = Get-Acl -LiteralPath $EnvFile
    $backup = Join-Path $BackupDirectory ('env-acl-' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff') + '.sddl')
    [IO.File]::WriteAllText($backup, $acl.Sddl)
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($rule in @($acl.Access)) { [void]$acl.RemoveAccessRuleSpecific($rule) }
    foreach ($entry in @(@('S-1-5-18','FullControl'), @('S-1-5-32-544','FullControl'), @('S-1-5-19','Read'))) {
        $sid = New-Object Security.Principal.SecurityIdentifier($entry[0])
        $rule = New-Object Security.AccessControl.FileSystemAccessRule($sid, $entry[1], 'Allow')
        [void]$acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $EnvFile -AclObject $acl
}

function Stop-ManagedService {
    param($Service, [int]$Timeout = 70)
    if ($null -ne $Service -and $Service.State -ne 'Stopped') {
        Stop-Service -Name $Service.Name
        (Get-Service -Name $Service.Name).WaitForStatus('Stopped', [TimeSpan]::FromSeconds($Timeout))
    }
}

function Write-ServiceConfig {
    param([string]$Path, [string]$Xml)
    [xml]$checked = $Xml
    if (-not $checked.service.id) { throw 'XML WinSW invalido.' }
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        Copy-Item -LiteralPath $Path -Destination ($Path + '.' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff') + '.bak')
    }
    [IO.File]::WriteAllText($Path, $Xml, (New-Object Text.UTF8Encoding($false)))
}

function Start-ManagedService {
    param([string]$Id, [string]$Wrapper, [bool]$AlreadyInstalled)
    if (-not $AlreadyInstalled) {
        & $Wrapper install
        if ($LASTEXITCODE -ne 0) { throw 'WinSW no pudo registrar el servicio. Revise el log local del wrapper.' }
    }
    # WinSW v2 reads XML at each start. No v3-only refresh command is used.
    Set-Service -Name $Id -StartupType Automatic
    & "$env:SystemRoot\System32\sc.exe" failure $Id reset= 3600 actions= restart/5000 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'No se pudo configurar recuperacion del servicio.' }
    & "$env:SystemRoot\System32\sc.exe" failureflag $Id 1 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'No se pudo configurar recuperacion ante salida con error.' }
    Start-Service -Name $Id
}

function New-ApiServiceXml {
    param([string]$Python, [string]$Project, [string]$LogPath)
    return @"
<service>
  <id>ApiVehiculoIoT</id>
  <name>API Vehiculo IoT</name>
  <description>FastAPI, un worker, RDS y WebSocket por vehiculo.</description>
  <executable>$(ConvertTo-XmlText $Python)</executable>
  <arguments>-m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1 --proxy-headers --forwarded-allow-ips 127.0.0.1 --timeout-graceful-shutdown 45 --no-use-colors --no-access-log</arguments>
  <workingdirectory>$(ConvertTo-XmlText $Project)</workingdirectory>
  <startmode>Automatic</startmode>
  <stoptimeout>60sec</stoptimeout>
  <onfailure action="restart" delay="5 sec" />
  <resetfailure>1 hour</resetfailure>
  <serviceaccount><domain>NT AUTHORITY</domain><user>LocalService</user></serviceaccount>
  <env name="PYTHONUNBUFFERED" value="1" />
  <env name="PYTHONDONTWRITEBYTECODE" value="1" />
  <logpath>$(ConvertTo-XmlText $LogPath)</logpath>
  <log mode="roll-by-size"><sizeThreshold>10240</sizeThreshold><keepFiles>5</keepFiles></log>
</service>
"@
}

function New-CaddyServiceXml {
    param([string]$Executable, [string]$ConfigPath, [string]$WorkingDirectory, [string]$LogPath)
    return @"
<service>
  <id>CaddyVehiculoIoT</id>
  <name>Caddy Vehiculo IoT HTTPS WSS</name>
  <description>Proxy HTTPS WSS hacia 127.0.0.1:8000; certificados persistentes.</description>
  <executable>$(ConvertTo-XmlText $Executable)</executable>
  <arguments>run --config &quot;$(ConvertTo-XmlText $ConfigPath)&quot; --adapter caddyfile</arguments>
  <workingdirectory>$(ConvertTo-XmlText $WorkingDirectory)</workingdirectory>
  <startmode>Automatic</startmode>
  <stoptimeout>30sec</stoptimeout>
  <onfailure action="restart" delay="5 sec" />
  <resetfailure>1 hour</resetfailure>
  <depend>ApiVehiculoIoT</depend>
  <serviceaccount><domain>NT AUTHORITY</domain><user>LocalService</user></serviceaccount>
  <logpath>$(ConvertTo-XmlText $LogPath)</logpath>
  <log mode="roll-by-size"><sizeThreshold>10240</sizeThreshold><keepFiles>5</keepFiles></log>
</service>
"@
}

function New-CaddyConfig {
    param([string]$Hostname, [string]$DataPath, [string]$AccessLog)
    # No wildcard, URI, port, path, whitespace or configuration injection.
    if ($Hostname.Length -gt 253 -or $Hostname -notmatch '^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z][A-Za-z0-9-]{1,62}$') {
        throw 'Hostname debe ser un nombre DNS completo, sin esquema, puerto ni ruta.'
    }
    foreach ($p in @($DataPath, $AccessLog)) {
        if ($p.IndexOfAny([char[]]@('"', "`r", "`n")) -ge 0) { throw 'Ruta invalida para Caddyfile.' }
    }
    $data = $DataPath.Replace('\','/')
    $access = $AccessLog.Replace('\','/')
    return @"
{
    admin off
    persist_config off
    storage file_system "$data"
    log default {
        output stdout
        format filter {
            request>headers delete
            resp_headers delete
            request>uri delete
        }
    }
}
$Hostname {
    reverse_proxy 127.0.0.1:8000
    log {
        output file "$access" {
            roll_size 10mb
            roll_keep 5
            roll_keep_for 168h
        }
        format filter {
            request>headers delete
            resp_headers delete
            request>uri delete
        }
    }
}
"@
}
