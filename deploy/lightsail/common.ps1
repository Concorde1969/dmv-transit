# Shared settings and helpers for the Lightsail scripts in this folder.
# Dot-sourced by create.ps1, remote.ps1 and push-env.ps1 - not run on its own.
# Works in Windows PowerShell 5.1 and PowerShell 7.

$ErrorActionPreference = 'Stop'

# --- Settings (override the profile with $env:AWS_PROFILE) --------------------------
$Region       = 'us-east-2'          # the project's selected Region (new AWS experience)
$Zone         = 'us-east-2a'
$AwsProfile      = if ($env:AWS_PROFILE) { $env:AWS_PROFILE } else { 'default' }
$InstanceName = 'dmvtransit'
$StaticIpName = 'dmvtransit-ip'
$Blueprint    = 'ubuntu_24_04'
$Bundle       = 'small_3_0'          # 2 GB RAM, 2 vCPU, 60 GB disk, public IPv4 (~$12/mo)
$SnapshotTime = '08:00'              # UTC (3-4 am Eastern) for automatic daily snapshots
$RemoteDir    = '/home/ubuntu/dmvtransit'

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path

function Invoke-Aws {
    <# Run an AWS CLI command in the configured Region/profile; return parsed JSON.
       Throws with the CLI's error text on failure. #>
    param([Parameter(Mandatory)][string[]]$Arguments, [switch]$AllowFailure)
    $errFile = [IO.Path]::GetTempFileName()
    # Windows PowerShell 5.1 turns a native command's stderr into terminating errors
    # under 'Stop'; check the exit code instead.
    $ErrorActionPreference = 'Continue'
    try {
        $out = & aws @Arguments --region $Region --profile $AwsProfile --output json 2> $errFile
        if ($LASTEXITCODE -ne 0) {
            if ($AllowFailure) { return $null }
            $msg = (Get-Content $errFile | Where-Object { $_ -match 'error occurred|\[ERROR\]' }) -join ' '
            if (-not $msg) { $msg = (Get-Content $errFile -Raw).Trim() }
            throw "aws $($Arguments[0..1] -join ' ') failed: $msg"
        }
        if ($out) { return ($out | Out-String | ConvertFrom-Json) }
    } finally { Remove-Item $errFile -ErrorAction SilentlyContinue }
}

function Get-MyPublicIp {
    (Invoke-RestMethod -Uri 'https://checkip.amazonaws.com' -TimeoutSec 15).Trim()
}

function Write-JsonFile([object]$Value) {
    <# JSON in a temp file (UTF-8, no BOM) for `file://` CLI parameters. #>
    $path = [IO.Path]::GetTempFileName()
    # -InputObject (not the pipeline) keeps a one-item array an array.
    [IO.File]::WriteAllText($path, (ConvertTo-Json -InputObject $Value -Depth 6),
                            (New-Object Text.UTF8Encoding $false))
    return $path
}

function Set-Firewall {
    <# Public ports: web for everyone; SSH only from Lightsail's browser SSH and this PC. #>
    $myIp = Get-MyPublicIp
    $ports = @(
        @{ fromPort = 80;  toPort = 80;  protocol = 'tcp'; cidrs = @('0.0.0.0/0'); ipv6Cidrs = @('::/0') },
        @{ fromPort = 443; toPort = 443; protocol = 'tcp'; cidrs = @('0.0.0.0/0'); ipv6Cidrs = @('::/0') },
        @{ fromPort = 443; toPort = 443; protocol = 'udp'; cidrs = @('0.0.0.0/0'); ipv6Cidrs = @('::/0') },
        @{ fromPort = 22;  toPort = 22;  protocol = 'tcp'; cidrs = @("$myIp/32");
           cidrListAliases = @('lightsail-connect') }
    )
    $file = Write-JsonFile $ports
    try {
        Invoke-Aws @('lightsail', 'put-instance-public-ports', '--instance-name', $InstanceName,
                     '--port-infos', "file://$file") | Out-Null
    } finally { Remove-Item $file -ErrorAction SilentlyContinue }
    return $myIp
}

function Assert-SshAllowedFromHere {
    <# Re-open SSH for this PC if its public IP changed since the firewall was set. #>
    $myIp = Get-MyPublicIp
    $state = Invoke-Aws @('lightsail', 'get-instance-port-states', '--instance-name', $InstanceName)
    $ssh = $state.portStates | Where-Object { $_.fromPort -eq 22 }
    if (-not ($ssh.cidrs -contains "$myIp/32")) {
        Write-Host "Your IP changed ($myIp) - updating the SSH firewall rule..."
        Set-Firewall | Out-Null
    }
}

function New-SshSession {
    <# Short-lived SSH credentials from Lightsail (no long-term key on this PC).
       Returns @{ Dir; Host; User; Args } - call Remove-SshSession afterwards. #>
    Assert-SshAllowedFromHere
    $d = (Invoke-Aws @('lightsail', 'get-instance-access-details', '--instance-name', $InstanceName,
                       '--protocol', 'ssh')).accessDetails
    $dir = Join-Path ([IO.Path]::GetTempPath()) ("dmvtransit-ssh-" + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $dir | Out-Null
    $utf8 = New-Object Text.UTF8Encoding $false
    $key = Join-Path $dir 'key'
    [IO.File]::WriteAllText($key, ($d.privateKey -replace "`r", ''), $utf8)
    [IO.File]::WriteAllText("$key-cert.pub", $d.certKey, $utf8)
    $known = ($d.hostKeys | ForEach-Object { "$($d.ipAddress) $($_.algorithm) $($_.publicKey)" }) -join "`n"
    [IO.File]::WriteAllText((Join-Path $dir 'known_hosts'), "$known`n", $utf8)
    # Lightsail reports the server's host keys once it has booted; until then, trust
    # the first key seen (this session only).
    $hostCheck = if ($d.hostKeys) { 'yes' } else { 'accept-new' }
    # OpenSSH refuses private keys other users can read.
    & icacls $key /inheritance:r /grant:r "$($env:USERNAME):R" | Out-Null
    $common = @('-i', $key, '-o', "CertificateFile=$key-cert.pub",
                '-o', "UserKnownHostsFile=$(Join-Path $dir 'known_hosts')",
                '-o', "StrictHostKeyChecking=$hostCheck", '-o', 'LogLevel=ERROR')
    return @{ Dir = $dir; Host = $d.ipAddress; User = $d.username; Args = $common }
}

function Remove-SshSession($Session) {
    if ($Session -and (Test-Path $Session.Dir)) {
        Remove-Item -Recurse -Force $Session.Dir -ErrorAction SilentlyContinue
    }
}

function Invoke-Remote($Session, [string]$Command) {
    <# Show the command's output; return only its exit code. #>
    $ErrorActionPreference = 'Continue'
    & ssh @($Session.Args) "$($Session.User)@$($Session.Host)" $Command | Out-Host
    return $LASTEXITCODE
}

function Copy-ToRemote($Session, [string]$LocalPath, [string]$RemotePath) {
    $ErrorActionPreference = 'Continue'
    & scp @($Session.Args) $LocalPath "$($Session.User)@$($Session.Host):$RemotePath"
    if ($LASTEXITCODE -ne 0) { throw "Upload to $RemotePath failed." }
}
