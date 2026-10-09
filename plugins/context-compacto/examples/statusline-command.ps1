# Minimal PowerShell status line for Claude Code, with the context-compacto
# auto-compact export baked in. For Windows users who don't already have a
# status line. Copy it somewhere (e.g. ~/.claude/) and point settings.json at it:
#
#   "statusLine": {
#     "type": "command",
#     "command": "powershell -NoProfile -ExecutionPolicy Bypass -File \"C:/Users/<you>/.claude/statusline-command.ps1\""
#   }
#
# ${CLAUDE_PLUGIN_ROOT} does NOT expand in statusLine.command, so this must live
# outside the plugin cache and be referenced by an absolute (or ~) path.
#
# It prints  "Model  used/size pct% [bar]  msgs:N"  and, when run inside tmux/psmux,
# drops ~/.claude/compacto-signals/<pane>.ctx every render so compacto-resume-daemon.sh
# can auto-/compact at your configured threshold. Delete the marked block to opt out.

$ErrorActionPreference = 'SilentlyContinue'

# msgs = total context tokens minus fixed system overhead (system prompt + tools +
# memory + skills). Calibrate once against your own /context, re-tune after big changes.
# This must match the number you want the daemon's default `msgs` metric to compare.
$SystemOverheadTokens = 32000

function Format-Tokens([double]$n) {
    if ($n -ge 1000000) { return ('{0:0.#}M' -f ($n / 1000000.0)) }
    if ($n -ge 1000)    { return ('{0:0.#}k' -f ($n / 1000.0)) }
    return [string][int]$n
}

# ----- read the JSON Claude Code pipes to the status line (once) -----
try { $data = [Console]::In.ReadToEnd() | ConvertFrom-Json -ErrorAction Stop }
catch { [Console]::Out.Write('Claude'); exit 0 }

$model = if ($data.model.display_name) { $data.model.display_name } else { 'Claude' }

$ctxUsed = 0
if ($null -ne $data.context_window.total_input_tokens) { $ctxUsed = [int64]$data.context_window.total_input_tokens }
$ctxSize = 0
if ($null -ne $data.context_window.context_window_size) { $ctxSize = [int64]$data.context_window.context_window_size }
$msgTokens = [int64][Math]::Max(0, $ctxUsed - $SystemOverheadTokens)

# progress bar (10 cells) against the real context-window size
$pct = 0; $filled = 0
if ($ctxSize -gt 0) {
    $pct    = [int][Math]::Round(($ctxUsed / [double]$ctxSize) * 100)
    $filled = [int][Math]::Floor(($ctxUsed / [double]$ctxSize) * 10)
}
if ($filled -lt 0) { $filled = 0 } elseif ($filled -gt 10) { $filled = 10 }
$bar = ('=' * $filled) + ('-' * (10 - $filled))

# --- context-compacto: export live context size for the auto-compact watcher ---
# Written every render, only inside tmux/psmux, keyed on the pane id. Atomic via
# tmp + Move. Never throws. Delete this whole block to opt out.
if ($env:TMUX_PANE) {
    try {
        $sigDir = Join-Path $HOME '.claude/compacto-signals'
        New-Item -ItemType Directory -Force -Path $sigDir | Out-Null
        $key  = $env:TMUX_PANE -replace '[^a-zA-Z0-9]', ''
        $line = "{0}`t{1}`t{2}" -f $env:TMUX_PANE, $ctxUsed, $msgTokens
        # psmux numbers panes per session, so its server pid (in $TMUX) joins the key and the line.
        if ($env:TMUX -match 'psmux-(\d+)') { $key = "$($Matches[1])_$key"; $line += "`t$($Matches[1])" }
        $tmp  = Join-Path $sigDir "$key.ctx.tmp"
        # No BOM (it would glue onto the pane id), hence WriteAllText rather than Out-File.
        [System.IO.File]::WriteAllText($tmp, "$line`n", (New-Object System.Text.UTF8Encoding($false)))
        Move-Item -Force -Path $tmp -Destination (Join-Path $sigDir "$key.ctx")
    } catch { }
}

[Console]::Out.Write(('{0}  {1}/{2} {3}% [{4}]  msgs:{5}' -f `
    $model, (Format-Tokens $ctxUsed), (Format-Tokens $ctxSize), $pct, $bar, (Format-Tokens $msgTokens)))
