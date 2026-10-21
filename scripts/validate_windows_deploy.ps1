#requires -Version 5.1
<#
.SYNOPSIS
Checks the deployment scripts without installing services, ACLs or firewall rules.
.DESCRIPTION
Run with Windows PowerShell 5.1. The installer guard is tested WITHOUT the
InstallOnThisServer switch. An optional report must use a NEW output path.
#>
[CmdletBinding()]
param([string]$OutputPath)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$project = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$directory = Join-Path $project 'deploy\windows'
$required = @('Windows-ServiceCommon.ps1', 'Install-ApiService.ps1', 'Install-CaddyService.ps1')
$entries = @()
foreach ($name in $required) {
    $path = Join-Path $directory $name
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Missing script: $name" }
    $tokens = $null
    $parseErrors = $null
    [void][Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$parseErrors)
    $entries += [pscustomobject]@{ script = $name; parser_errors = @($parseErrors).Count }
}
if (@($entries | Where-Object { $_.parser_errors -ne 0 }).Count -ne 0) {
    throw 'Deployment scripts have parser errors. No installers were executed.'
}
$guards = @()
foreach ($name in @('Install-ApiService.ps1', 'Install-CaddyService.ps1')) {
    $info = New-Object Diagnostics.ProcessStartInfo
    $info.FileName = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $info.Arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + (Join-Path $directory $name) + '"'
    if ($name -eq 'Install-CaddyService.ps1') { $info.Arguments += ' -Hostname example.invalid' }
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $info
    [void]$process.Start()
    $stdout = $process.StandardOutput.ReadToEnd()
    $stderr = $process.StandardError.ReadToEnd()
    $process.WaitForExit()
    $ok = $process.ExitCode -ne 0 -and ($stdout + $stderr) -match 'InstallOnThisServer'
    $guards += [pscustomobject]@{ script = $name; refused_without_install_switch = $ok }
    $process.Dispose()
    if (-not $ok) { throw "Installer guard did not refuse a run without its install switch: $name" }
}
$report = [ordered]@{
    checked_at_utc = [DateTime]::UtcNow.ToString('o')
    powershell_version = $PSVersionTable.PSVersion.ToString()
    parser = $entries
    guards = $guards
    services_installed = 0
    acl_changes = 0
    firewall_changes = 0
    rds_connections = 0
    public_https_wss_tested = $false
}
if ($OutputPath) {
    $fullOutput = [IO.Path]::GetFullPath($OutputPath)
    $stream = New-Object IO.FileStream($fullOutput, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write)
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes(($report | ConvertTo-Json -Depth 6) + [Environment]::NewLine)
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush($true)
    } finally { $stream.Dispose() }
}
$report | ConvertTo-Json -Depth 6
