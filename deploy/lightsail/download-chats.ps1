<#
.SYNOPSIS
  Download saved chats from dmvtransit.com to this PC as a spreadsheet (CSV) and a
  detailed JSON-lines file.

.DESCRIPTION
  Runs `transit-mcp chatlog export` on the server and saves the results in the
  chat-exports folder of the project (git-ignored - these files contain what
  visitors typed, so keep them private). Also prints totals for the period.

  CSV: one row per question - time, conversation, anonymous visitor, device,
       question, answer, outcome, tools used, tokens, seconds, error.
  JSONL: everything, including what each data tool was asked and how long it took.

.PARAMETER Days
  How many days back to download (default 30). Ignored when -Since is given.

.PARAMETER Since
  First day to include, YYYY-MM-DD.

.PARAMETER Until
  Day to stop before, YYYY-MM-DD (default: no end).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File deploy\lightsail\download-chats.ps1
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File deploy\lightsail\download-chats.ps1 -Since 2026-10-01 -Until 2026-11-01
#>
param(
    [int]$Days = 30,
    [string]$Since,
    [string]$Until
)

. (Join-Path $PSScriptRoot 'common.ps1')

if (-not $Since) { $Since = (Get-Date).AddDays(-$Days).ToString('yyyy-MM-dd') }
foreach ($d in @($Since, $Until)) {
    if ($d -and $d -notmatch '^\d{4}-\d{2}-\d{2}$') { throw "Dates must look like 2026-10-01 (got '$d')." }
}
$range = "--since $Since" + $(if ($Until) { " --until $Until" } else { '' })
$label = "$Since" + $(if ($Until) { "_to_$Until" } else { "_to_$((Get-Date).ToString('yyyy-MM-dd'))" })

$outDir = Join-Path $RepoRoot 'chat-exports'
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
$stamp = [guid]::NewGuid().ToString('N').Substring(0, 8)
$remote = "/tmp/chats-$stamp"

$session = New-SshSession
try {
    $code = Invoke-Remote $session ("cd $RemoteDir && umask 077 && " +
        "docker compose exec -T app transit-mcp chatlog export --format csv $range > $remote.csv && " +
        "docker compose exec -T app transit-mcp chatlog export --format jsonl $range > $remote.jsonl && " +
        "docker compose exec -T app transit-mcp chatlog stats --days $Days")
    if ($code -ne 0) { throw "Export on the server failed (exit $code)." }
    foreach ($ext in 'csv', 'jsonl') {
        $local = Join-Path $outDir "chats-$label.$ext"
        Copy-FromRemote $session "$remote.$ext" $local
        Write-Host "Saved $local"
    }
} finally {
    if ($session) { Invoke-Remote $session "rm -f $remote.csv $remote.jsonl" | Out-Null }
    Remove-SshSession $session
}
