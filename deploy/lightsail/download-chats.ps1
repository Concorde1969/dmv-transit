<#
.SYNOPSIS
  Download the site's usage data from dmvtransit.com to this PC: saved chats, map trip
  searches and page activity, each as a spreadsheet (CSV) and a detailed JSON-lines file.

.DESCRIPTION
  Runs `transit-mcp chatlog export` on the server and saves the results in the
  chat-exports folder of the project (git-ignored - these files contain what
  visitors typed, so keep them private). Also prints totals for the period.

  chats-*.csv     one row per question - time, conversation, anonymous visitor, device,
                  question, answer, outcome, tools used, tokens, seconds, error.
  searches-*.csv  one row per map search - when (with weekday and hour), visitor, device,
                  how start/end were chosen, start/end rounded to ~100 m, landmark names,
                  nearest stop and rail station at each end, distance, outcome, number of
                  options, the best option (minutes, leave-in, walking, changes, routes),
                  agencies, live coverage, rider reports shown, every option (JSON), timing.
  events-*.csv    page activity - visits (referring site), address searches (result
                  counts), options tapped and opened, assistant / report form opened,
                  "ask about this trip", reports sent, location allowed or denied.
  *.jsonl         the same with full detail (e.g. what each data tool was asked).
  The visitor ID links one visitor's chats, searches and activity.

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
    $tables = 'chats', 'searches', 'events'
    $cmd = "cd $RemoteDir && umask 077"
    foreach ($t in $tables) {
        foreach ($ext in 'csv', 'jsonl') {
            $cmd += " && docker compose exec -T app transit-mcp chatlog export --table $t --format $ext $range > $remote-$t.$ext"
        }
    }
    $code = Invoke-Remote $session ($cmd + " && docker compose exec -T app transit-mcp chatlog stats --days $Days")
    if ($code -ne 0) { throw "Export on the server failed (exit $code)." }
    foreach ($t in $tables) {
        foreach ($ext in 'csv', 'jsonl') {
            $local = Join-Path $outDir "$t-$label.$ext"
            Copy-FromRemote $session "$remote-$t.$ext" $local
            Write-Host "Saved $local"
        }
    }
} finally {
    if ($session) { Invoke-Remote $session "rm -f $remote-*" | Out-Null }
    Remove-SshSession $session
}
