#!/usr/bin/env pwsh
# claude-resume.ps1: bring Claude Code back into its psmux panes after a reboot.
#
# psmux-resurrect restores sessions, windows, panes and their directories, but it can only re-type
# a pane's program name, and for a Claude pane psmux reports an MCP child ("node"). This fills the
# gap the way tmux-resurrect hooks do on macOS:
#
#   -Loop      every @claude-resume-save-interval minutes (default 5): psmux-resurrect's save, then
#              a snapshot of each Claude pane (slot, directory, command line, session id) to
#              ~/.psmux/claude-resume/last.json. One loop per user; it ends with the last session.
#   -Restore   only on a fresh start (no other psmux session alive, or -Force): psmux-resurrect's
#              restore, then -Resume. Turn it off with: set -g @claude-resume-restore 'off'
#   -Resume    into each saved slot that sits at a PowerShell prompt with no Claude in it, types
#                Clear-Host
#                Set-Location -LiteralPath '<dir>'; & '<claude.exe>' <its flags> --resume '<id>'
#              one pane per second. -DryRun logs the commands instead.
#   -Snapshot  writes last.json now.
#
# Logs to ~/.psmux/claude-resume/claude-resume.log. The ~/.psmux.conf wiring is in the
# context-compacto README, "Windows (psmux)".
[CmdletBinding()]
param(
    [switch]$Loop,
    [switch]$Restore,
    [switch]$Resume,
    [switch]$Snapshot,
    [switch]$DryRun,
    [switch]$Force,
    [double]$IntervalMinutes = 0,
    [string]$StateFile = (Join-Path $env:USERPROFILE '.psmux\claude-resume\last.json')
)

$ErrorActionPreference = 'Continue'
$StateDir = Split-Path -Parent $StateFile
$LogFile = Join-Path $StateDir 'claude-resume.log'
$ResurrectScripts = Join-Path $env:USERPROFILE '.psmux\plugins\psmux-resurrect\scripts'
New-Item -ItemType Directory -Force -Path $StateDir | Out-Null

# run-shell shows a hook's output in the pane, so everything goes to the log instead.
function Write-Log([string]$Message) {
    try { Add-Content -LiteralPath $LogFile -Value ('{0} [{1}] {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $PID, $Message) } catch {}
}

function Get-PsmuxBin {
    foreach ($n in 'psmux', 'pmux', 'tmux') {
        $b = Get-Command $n -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($b) { return $b.Source }
    }
    return 'psmux'
}
$PSMUX = Get-PsmuxBin

function Get-PsmuxOption([string]$Name, [string]$Default) {
    $v = (& $PSMUX show-options -gv $Name 2>$null | Out-String).Trim()
    if ($LASTEXITCODE -eq 0 -and $v) { return $v }
    return $Default
}

# psmux-resurrect needs PowerShell 7. A hook gets the PATH psmux started with, which can lack the WindowsApps
# folder holding the Store pwsh's launcher; psmux then quietly runs the hook in Windows PowerShell instead.
function Get-Pwsh {
    $c = Get-Command pwsh -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($c) { return $c.Source }
    foreach ($p in (Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps\pwsh.exe'), (Join-Path $env:ProgramFiles 'PowerShell\7\pwsh.exe')) {
        if (Test-Path -LiteralPath $p) { return $p }
    }
}

# Sessions other than psmux's own standby (__warm__ and the like).
function Get-UserSessions {
    $names = & $PSMUX list-sessions -F '#{session_name}' 2>$null
    if ($LASTEXITCODE -ne 0) { return @() }
    return @($names | Where-Object { $_ -and -not $_.StartsWith('__') })
}

function Split-CommandLine([string]$Line) {
    if (-not ('ClaudeResume.Native' -as [type])) {
        Add-Type -Namespace ClaudeResume -Name Native -MemberDefinition @'
[DllImport("shell32.dll", CharSet = CharSet.Unicode)] public static extern IntPtr CommandLineToArgvW(string cmdLine, out int numArgs);
[DllImport("kernel32.dll")] public static extern IntPtr LocalFree(IntPtr mem);
'@
    }
    $n = 0
    $ptr = [ClaudeResume.Native]::CommandLineToArgvW($Line, [ref]$n)
    if ($ptr -eq [IntPtr]::Zero) { return @() }
    try {
        for ($i = 0; $i -lt $n; $i++) {
            [Runtime.InteropServices.Marshal]::PtrToStringUni([Runtime.InteropServices.Marshal]::ReadIntPtr($ptr, $i * [IntPtr]::Size))
        }
    } finally { [void][ClaudeResume.Native]::LocalFree($ptr) }
}

# Claude's flags minus the ones that pick a conversation (the restore adds its own --resume) and
# minus a launch prompt, which would otherwise be sent again into the resumed conversation. Names
# follow `claude --help`; an unlisted option (several are hidden) is taken to have one value.
$BoolFlags = @('--allow-dangerously-skip-permissions', '--ax-screen-reader', '--bg', '--background', '--bare',
    '--brief', '--chrome', '--dangerously-skip-permissions', '--desktop', '--disable-slash-commands',
    '--exclude-dynamic-system-prompt-sections', '--forward-subagent-text', '--ide', '--include-hook-events',
    '--include-partial-messages', '--no-chrome', '--no-session-persistence', '--replay-user-messages',
    '--restricted', '--safe-mode', '--strict-mcp-config', '--tmux', '--verbose')
$ListFlags = @('--add-dir', '--allowedTools', '--allowed-tools', '--betas', '--disallowedTools',
    '--disallowed-tools', '--file', '--mcp-config', '--tools')

function Get-RelaunchArgs([string[]]$Argv) {
    $i = 0
    while ($i -lt $Argv.Count) {
        $a = $Argv[$i]; $i++
        if (-not $a.StartsWith('-')) { continue }                       # the launch prompt
        $name = $a.Split('=')[0]
        if ($name -in '-c', '--continue', '--fork-session') { continue }
        if ($name -in '-r', '--resume', '--session-id') {
            if (-not $a.Contains('=') -and $i -lt $Argv.Count -and -not $Argv[$i].StartsWith('-')) { $i++ }
            continue
        }
        $a
        if ($a.Contains('=') -or $BoolFlags -contains $name) { continue }
        while ($i -lt $Argv.Count -and -not $Argv[$i].StartsWith('-')) {
            $Argv[$i]; $i++
            if ($ListFlags -notcontains $name) { break }
        }
    }
}

# Every live interactive Claude that runs in a psmux pane, by the slot psmux-resurrect restores
# (session, window index, pane index). Claude Code keeps ~/.claude/sessions/<pid>.json per process.
function Get-ClaudePanes {
    $slots = @{}
    foreach ($l in @(& $PSMUX list-panes -a -F '#{session_name}|#{window_index}|#{pane_index}|#{pane_pid}' 2>$null)) {
        $f = "$l".Split('|')
        if ($f.Count -eq 4 -and $f[3] -match '^\d+$') { $slots[[int]$f[3]] = $f }
    }
    $procs = @{}
    foreach ($p in Get-CimInstance Win32_Process -Property ProcessId, ParentProcessId, CommandLine) { $procs[[int]$p.ProcessId] = $p }
    $found = foreach ($file in @(Get-ChildItem -LiteralPath (Join-Path $env:USERPROFILE '.claude\sessions') -Filter '*.json' -ErrorAction SilentlyContinue)) {
        try { $s = Get-Content -Raw -LiteralPath $file.FullName | ConvertFrom-Json } catch { continue }
        if (-not $s.sessionId -or ($s.kind -and $s.kind -ne 'interactive')) { continue }
        # A crash can leave the file behind with its pid reused by something else.
        $p = $procs[[int]$s.pid]
        if (-not $p -or "$($p.CommandLine)" -notmatch 'claude') { continue }
        $slot = $null; $cur = $p
        for ($hop = 0; $hop -lt 6 -and $cur -and -not $slot; $hop++) {
            $slot = $slots[[int]$cur.ParentProcessId]
            $cur = $procs[[int]$cur.ParentProcessId]
        }
        if (-not $slot) { continue }
        $argv = @(Split-CommandLine $p.CommandLine)
        if ($argv.Count -lt 1) { continue }
        [pscustomobject]@{
            session   = $slot[0]
            window    = [int]$slot[1]
            pane      = [int]$slot[2]
            cwd       = [string]$s.cwd
            exe       = $argv[0]
            args      = @(Get-RelaunchArgs @($argv | Select-Object -Skip 1))
            sessionId = [string]$s.sessionId
        }
    }
    return @($found | Sort-Object session, window, pane)
}

function Save-Snapshot {
    $panes = @(Get-ClaudePanes)
    $json = ConvertTo-Json -Depth 5 -InputObject ([ordered]@{ savedAt = (Get-Date).ToString('o'); panes = $panes })
    # The previous snapshot stays as prev.json, for a manual retry if a save caught a shutdown.
    if (Test-Path -LiteralPath $StateFile) { Copy-Item -Force -LiteralPath $StateFile -Destination (Join-Path $StateDir 'prev.json') }
    Set-Content -LiteralPath "$StateFile.tmp" -Value $json -Encoding utf8
    Move-Item -Force -LiteralPath "$StateFile.tmp" -Destination $StateFile
    Write-Log ('snapshot: {0} Claude pane(s){1}' -f $panes.Count, (($panes | ForEach-Object { " $($_.session):$($_.window).$($_.pane)=$($_.sessionId)" }) -join ''))
}

function Format-PsArg([string]$s) { "'" + $s.Replace("'", "''") + "'" }

function Invoke-Resume {
    if (-not (Test-Path -LiteralPath $StateFile)) { Write-Log 'resume: no snapshot yet'; return }
    try { $state = Get-Content -Raw -LiteralPath $StateFile | ConvertFrom-Json } catch { Write-Log "resume: unreadable snapshot: $_"; return }
    $live = @(Get-ClaudePanes)
    $started = @{}
    Start-Sleep -Seconds 2
    foreach ($e in @($state.panes)) {
        $target = '={0}:{1}.{2}' -f $e.session, $e.window, $e.pane
        $cmdName = (& $PSMUX display-message -p -t $target '#{pane_current_command}' 2>$null | Out-String).Trim()
        if ($LASTEXITCODE -ne 0) { Write-Log "skip ${target}: no such pane"; continue }
        if ($cmdName -notin 'powershell', 'pwsh') { Write-Log "skip ${target}: not at a PowerShell prompt ($cmdName)"; continue }
        if (@($live | Where-Object { $_.session -eq $e.session -and $_.window -eq $e.window -and $_.pane -eq $e.pane }).Count) {
            Write-Log "skip ${target}: Claude is already running there"; continue
        }
        # Two Claude processes on one conversation would both append to its transcript. A snapshot lists one
        # twice when it was resumed in two panes; only the first of them gets it back.
        if ($started[$e.sessionId] -or @($live | Where-Object { $_.sessionId -eq $e.sessionId }).Count) { Write-Log "skip ${target}: $($e.sessionId) is open elsewhere"; continue }
        $started[$e.sessionId] = $true
        $line = 'Set-Location -LiteralPath {0} -ErrorAction SilentlyContinue; & {1}' -f (Format-PsArg $e.cwd), (Format-PsArg $e.exe)
        foreach ($a in @($e.args)) { $line += ' ' + (Format-PsArg $a) }
        $line += ' --resume ' + (Format-PsArg $e.sessionId)
        if ($DryRun) { Write-Log "dry-run ${target}: $line"; continue }
        & $PSMUX send-keys -t $target 'Clear-Host' Enter 2>$null
        & $PSMUX send-keys -t $target $line Enter 2>$null
        Write-Log "resumed ${target}: $($e.sessionId)"
        Start-Sleep -Seconds 1
    }
}

# Never throws: a failed save or restore is logged and the caller goes on to the Claude part.
function Invoke-Resurrect([string]$Name) {
    try {
        $file = Join-Path $ResurrectScripts "$Name.ps1"
        if (-not (Test-Path -LiteralPath $file)) { Write-Log "${Name}: psmux-resurrect is not installed"; return }
        $pwsh = Get-Pwsh
        if (-not $pwsh) { Write-Log "${Name}: skipped, PowerShell 7 (pwsh) not found"; return }
        $out = & $pwsh -NoProfile -File $file 2>&1 | Out-String
        if ($LASTEXITCODE) { Write-Log ('{0}: psmux-resurrect exited {1}: {2}' -f $Name, $LASTEXITCODE, ($out.Trim() -split "`n")[-1]) }
    } catch { Write-Log "${Name}: $_" }
}

function Invoke-Restore {
    if ((Get-PsmuxOption '@claude-resume-restore' 'on') -eq 'off') { return }
    $sessions = @(Get-UserSessions)
    if ($sessions.Count -gt 1 -and -not $Force) { Write-Log "restore: skipped, psmux was already running ($($sessions -join ', '))"; return }
    Write-Log "restore: fresh start ($($sessions -join ', '))"
    Invoke-Resurrect 'restore'
    Invoke-Resume
}

function Invoke-Loop {
    # psmux runs a server per session and each one fires client-attached; one loop serves them all.
    $mutex = New-Object System.Threading.Mutex($false, 'Local\psmux-claude-resume')
    try { $mine = $mutex.WaitOne(20000) } catch [System.Threading.AbandonedMutexException] { $mine = $true }
    if (-not $mine) { return }
    Write-Log 'loop: started'
    try {
        while ($true) {
            $minutes = $IntervalMinutes
            if ($minutes -le 0) {
                $opt = Get-PsmuxOption '@claude-resume-save-interval' '5'
                $minutes = if ($opt -match '^\d+(\.\d+)?$') { [double]$opt } else { 5 }
            }
            if ($minutes -le 0) { Write-Log 'loop: saving is off (@claude-resume-save-interval 0)'; break }
            $due = (Get-Date).AddMinutes($minutes)
            while ((Get-Date) -lt $due -and @(Get-UserSessions).Count) { Start-Sleep -Seconds ([Math]::Min(10, [Math]::Max(1, ($due - (Get-Date)).TotalSeconds))) }
            if (-not @(Get-UserSessions).Count) { Write-Log 'loop: no psmux session left, stopping'; break }
            Invoke-Resurrect 'save'
            # An error escaping here would end the loop, and with it all saving until psmux restarts.
            try { Save-Snapshot } catch { Write-Log "snapshot: $_" }
        }
    } finally {
        try { $mutex.ReleaseMutex() } catch {}
        $mutex.Dispose()
    }
}

try {
    if ($Snapshot) { Save-Snapshot }
    if ($Resume) { Invoke-Resume }
    if ($Restore) { Invoke-Restore }
    if ($Loop) { Invoke-Loop }
} catch {
    Write-Log "error: $_"
}
