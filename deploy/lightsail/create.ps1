<#
.SYNOPSIS
  Create (or finish creating) the dmvtransit Lightsail server in us-east-2.

.DESCRIPTION
  Safe to run again - each step is skipped if it's already done:
    1. Instance 'dmvtransit' (Ubuntu 24.04, 2 GB plan) with automatic daily snapshots.
       On first boot it clones the repo and installs Docker (deploy/lightsail/user-data.sh).
    2. Static IP 'dmvtransit-ip', attached to the instance.
    3. Firewall: HTTP 80 and HTTPS 443 open; SSH 22 only from Lightsail's browser SSH
       and this PC's current IP.
  Costs about $12/month while the instance exists.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File deploy\lightsail\create.ps1
#>
. (Join-Path $PSScriptRoot 'common.ps1')

Write-Host "Region $Region, profile '$AwsProfile'"

# --- 1. Instance -------------------------------------------------------------------
$existing = Invoke-Aws @('lightsail', 'get-instance', '--instance-name', $InstanceName) -AllowFailure
if ($existing) {
    Write-Host "Instance '$InstanceName' already exists ($($existing.instance.state.name))."
} else {
    Write-Host "Creating instance '$InstanceName' ($Bundle, $Blueprint) in $Zone..."
    # User data must reach Linux with LF line endings.
    $userData = [IO.File]::ReadAllText((Join-Path $PSScriptRoot 'user-data.sh')) -replace "`r`n", "`n"
    $udFile = [IO.Path]::GetTempFileName()
    [IO.File]::WriteAllText($udFile, $userData, (New-Object Text.UTF8Encoding $false))
    $addOns = Write-JsonFile @(@{ addOnType = 'AutoSnapshot';
                                  autoSnapshotAddOnRequest = @{ snapshotTimeOfDay = $SnapshotTime } })
    try {
        Invoke-Aws @('lightsail', 'create-instances', '--instance-names', $InstanceName,
                     '--availability-zone', $Zone, '--blueprint-id', $Blueprint,
                     '--bundle-id', $Bundle, '--user-data', "file://$udFile",
                     '--add-ons', "file://$addOns",
                     '--tags', 'key=project,value=dmvtransit') | Out-Null
    } finally { Remove-Item $udFile, $addOns -ErrorAction SilentlyContinue }
}

Write-Host -NoNewline 'Waiting for the instance to be running'
for ($i = 0; $i -lt 60; $i++) {
    $state = (Invoke-Aws @('lightsail', 'get-instance', '--instance-name', $InstanceName)).instance.state.name
    if ($state -eq 'running') { break }
    Write-Host -NoNewline '.'
    Start-Sleep -Seconds 5
}
Write-Host " $state"
if ($state -ne 'running') { throw "Instance is '$state' after 5 minutes - check the Lightsail console." }

# --- 2. Static IP ------------------------------------------------------------------
$ip = Invoke-Aws @('lightsail', 'get-static-ip', '--static-ip-name', $StaticIpName) -AllowFailure
if (-not $ip) {
    Write-Host "Allocating static IP '$StaticIpName'..."
    Invoke-Aws @('lightsail', 'allocate-static-ip', '--static-ip-name', $StaticIpName) | Out-Null
    $ip = Invoke-Aws @('lightsail', 'get-static-ip', '--static-ip-name', $StaticIpName)
}
if (-not $ip.staticIp.isAttached) {
    Write-Host "Attaching '$StaticIpName' to '$InstanceName'..."
    Invoke-Aws @('lightsail', 'attach-static-ip', '--static-ip-name', $StaticIpName,
                 '--instance-name', $InstanceName) | Out-Null
    $ip = Invoke-Aws @('lightsail', 'get-static-ip', '--static-ip-name', $StaticIpName)
} elseif ($ip.staticIp.attachedTo -ne $InstanceName) {
    throw "Static IP '$StaticIpName' is attached to '$($ip.staticIp.attachedTo)', not '$InstanceName'."
}
$address = $ip.staticIp.ipAddress

# --- 3. Firewall -------------------------------------------------------------------
$myIp = Set-Firewall
Write-Host "Firewall set: 80/443 open; SSH from Lightsail browser SSH and $myIp."

$sslip = ($address -replace '\.', '-') + '.sslip.io'
Write-Host ""
Write-Host "Done. Static IP: $address"
Write-Host "First-boot setup (Docker, clone) takes a few minutes. Check it with:"
Write-Host "  powershell -ExecutionPolicy Bypass -File deploy\lightsail\remote.ps1 'cloud-init status --wait'"
Write-Host "Test hostname before the domain exists: $sslip"
