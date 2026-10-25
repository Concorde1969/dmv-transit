<#
.SYNOPSIS
  Run a command on the dmvtransit server, or upload a file to it, over SSH with
  short-lived credentials from Lightsail (nothing permanent is stored on this PC).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File deploy\lightsail\remote.ps1 'cd ~/dmvtransit && docker compose ps'
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File deploy\lightsail\remote.ps1 'bash ~/dmvtransit/deploy/update.sh'
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File deploy\lightsail\remote.ps1 -Upload .\google_transit.zip -To '~/dmvtransit/feeds/cue/google_transit.zip'
#>
[CmdletBinding(DefaultParameterSetName = 'Run')]
param(
    [Parameter(Mandatory, Position = 0, ParameterSetName = 'Run')][string]$Command,
    [Parameter(Mandatory, ParameterSetName = 'Upload')][string]$Upload,
    [Parameter(Mandatory, ParameterSetName = 'Upload')][string]$To
)

. (Join-Path $PSScriptRoot 'common.ps1')

$session = New-SshSession
try {
    if ($PSCmdlet.ParameterSetName -eq 'Upload') {
        Copy-ToRemote $session (Resolve-Path $Upload).Path $To
        Write-Host "Uploaded to $To"
        $code = 0
    } else {
        $code = Invoke-Remote $session $Command
    }
} finally {
    Remove-SshSession $session
}
exit $code
