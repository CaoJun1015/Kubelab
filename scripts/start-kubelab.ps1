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

$wslDirectoryOutput = & wsl.exe -d $Distribution --cd $PSScriptRoot -- pwd
$wslExitCode = $LASTEXITCODE
$wslDirectory = $wslDirectoryOutput | Select-Object -Last 1
if ($wslExitCode -ne 0 -or [string]::IsNullOrWhiteSpace([string]$wslDirectory)) {
    throw "Could not resolve the script directory inside WSL distribution '$Distribution'."
}
$wslScript = ([string]$wslDirectory).TrimEnd("/") + "/start_kubelab.sh"

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
