<#
.SYNOPSIS
  Send the server its settings (.env) and start the site. Run this yourself - it
  copies your API keys from your local .env without displaying them.

.DESCRIPTION
  Builds the server's .env from deploy/.env.production.example, filling in:
    ANTHROPIC_API_KEY, WMATA_API_KEY (required), TRANSITLAND_API_KEY,
    GOOGLE_GEOCODING_API_KEY, GEOCODE_API_KEY, SMTP_USERNAME and SMTP_PASSWORD
      - copied from the .env in the project folder on this PC;
    SITE_DOMAIN and ACME_EMAIL - from the parameters below.
  Uploads it to the server (readable only by the server's user), then starts or
  restarts the site with `docker compose up -d --build`.

.PARAMETER SiteDomain
  The address the site answers on, e.g. dmvtransit.com. Before you own the domain,
  use the test hostname create.ps1 printed (e.g. 3-14-15-92.sslip.io).

.PARAMETER AcmeEmail
  Where Let's Encrypt sends certificate notices.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File deploy\lightsail\push-env.ps1 -SiteDomain dmvtransit.com -AcmeEmail you@example.com
#>
param(
    [Parameter(Mandatory)][string]$SiteDomain,
    [Parameter(Mandatory)][string]$AcmeEmail
)

. (Join-Path $PSScriptRoot 'common.ps1')

function Read-EnvFile([string]$Path) {
    $values = @{}
    foreach ($line in [IO.File]::ReadAllLines($Path)) {
        if ($line -match '^\s*([A-Z0-9_]+)\s*=\s*(.*)$') {
            $values[$Matches[1]] = $Matches[2].Trim().Trim('"').Trim("'")
        }
    }
    return $values
}

$localEnv = Join-Path $RepoRoot '.env'
if (-not (Test-Path $localEnv)) { throw "No .env found at $localEnv." }
$local = Read-EnvFile $localEnv

$fill = @{ SITE_DOMAIN = $SiteDomain.Trim(); ACME_EMAIL = $AcmeEmail.Trim() }
foreach ($name in 'ANTHROPIC_API_KEY', 'WMATA_API_KEY', 'TRANSITLAND_API_KEY',
                 'GOOGLE_GEOCODING_API_KEY', 'GOOGLE_GEOCODING_MONTHLY_BUDGET',
                 'GOOGLE_PLACES_MONTHLY_BUDGET', 'GEOCODE_API_KEY',
                 'SMTP_HOST', 'SMTP_USERNAME', 'SMTP_PASSWORD', 'MAIL_FROM') {
    if ($local[$name]) { $fill[$name] = $local[$name] }
}
$missing = @('ANTHROPIC_API_KEY', 'WMATA_API_KEY') | Where-Object { -not $fill[$_] }
if ($missing) { throw "Missing in your local .env: $($missing -join ', ')" }

# Template lines like `NAME=` get our value; everything else is kept as written.
$template = [IO.File]::ReadAllLines((Join-Path $RepoRoot 'deploy\.env.production.example'))
$lines = foreach ($line in $template) {
    if ($line -match '^([A-Z0-9_]+)=' -and $fill.ContainsKey($Matches[1])) {
        "$($Matches[1])=$($fill[$Matches[1]])"
    } else { $line }
}

$tmp = Join-Path ([IO.Path]::GetTempPath()) ("dmvtransit-env-" + [guid]::NewGuid().ToString('N'))
$session = $null
try {
    [IO.File]::WriteAllText($tmp, (($lines -join "`n") + "`n"), (New-Object Text.UTF8Encoding $false))
    & icacls $tmp /inheritance:r /grant:r "$($env:USERNAME):F" | Out-Null

    $session = New-SshSession
    Write-Host "Uploading settings to the server (keys are not displayed)..."
    Copy-ToRemote $session $tmp "$RemoteDir/.env.new"
    $code = Invoke-Remote $session ("cd $RemoteDir && chmod 600 .env.new && mv .env.new .env " +
                                    "&& docker compose up -d --build && docker compose ps")
    if ($code -ne 0) { throw "Starting the site failed (exit $code)." }
    Write-Host ""
    Write-Host "Site starting at https://$SiteDomain - the first data load takes about 10 minutes."
    Write-Host "Watch it: powershell -ExecutionPolicy Bypass -File deploy\lightsail\remote.ps1 'cd ~/dmvtransit && docker compose logs --tail 40 scheduler'"
} finally {
    Remove-Item $tmp -Force -ErrorAction SilentlyContinue
    Remove-SshSession $session
}
