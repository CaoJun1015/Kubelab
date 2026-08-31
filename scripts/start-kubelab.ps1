[CmdletBinding()]
param(
    [ValidateSet("Start", "Status", "Stop")]
    [string]$Action = "Start",
    [ValidatePattern("^[A-Za-z0-9._-]+$")]
    [string]$Distribution = "Ubuntu",
    [switch]$WebOnly,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$webUrl = "http://127.0.0.1:8765/"
$bashScript = Join-Path $PSScriptRoot "start_kubelab.sh"

if (-not (Test-Path -LiteralPath $bashScript -PathType Leaf)) {
    throw "WSL startup script was not found: $bashScript"
}
if ($Action -ne "Start" -and $WebOnly) {
    throw "-WebOnly can only be used with -Action Start."
}

$wslScript = (& wsl.exe -d $Distribution -- wslpath -a $bashScript).Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($wslScript)) {
    throw "Could not resolve the startup script inside WSL distribution '$Distribution'."
}

$wslArguments = @("-d", $Distribution, "--", "bash", $wslScript)
switch ($Action) {
    "Status" { $wslArguments += "--status" }
    "Stop" { $wslArguments += "--stop" }
    "Start" {
        if ($WebOnly) {
            $wslArguments += "--web-only"
        }
    }
}

& wsl.exe @wslArguments
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

if ($Action -eq "Start" -and -not $NoBrowser) {
    Start-Process $webUrl
}
