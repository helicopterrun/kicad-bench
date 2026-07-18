#Requires -Version 5.1
<#
.SYNOPSIS
    Bootstrap the KiCad PCB-design stack on a fresh Windows 10/11 machine, wired for
    BOTH the Claude Desktop app and Claude Code.

.DESCRIPTION
    One script that installs and wires up everything a coworker needs to design PCBs
    with our stack:

      * KiCad 10        - the EDA app + kicad-cli (the headless backbone every gate uses)
      * Python 3.12     - runs kicad-bench and the design-block generator
      * Git             - clone/update the repos
      * Claude Desktop  - the GUI app, wired with the kicad-sch-api MCP  (-SkipClaudeDesktop)
      * Claude Code     - the CLI, wired with the kb skills + kicad-sch-api MCP (-SkipClaudeCode)
      * kicad-bench     - the `kb` quality/DFM/lifecycle gate CLI, installed via pipx
      * design-block-generator - the skill+tooling to make your OWN reusable blocks

    The AI plumbing is shared: the kicad-sch-api MCP server is registered for whichever
    Claude surfaces you install. Claude Code also gets our filesystem SKILL.md skills
    linked in; because Claude Desktop can't load filesystem skills, the script also drops
    ready-to-upload skill .zip bundles for you to add via Desktop's Settings.

    Bring-your-own personalization (no shared branding): you're prompted for your own
    title block / drawing sheet, and the script scans for any design-block libraries you
    already have. Nothing private is cloned.

    Idempotent: anything already present is detected and skipped, so you can re-run it
    any time to pull repo updates and re-sync the Claude skills / MCP config.

    NOTE: winget may pop UAC elevation prompts when installing KiCad/Python/Git and the
    Claude apps. Everything else (repos, pip, skills, MCP config, env vars) is per-user
    and needs no admin.

.PARAMETER Workspace
    Where to clone the repos. Default: $HOME\kicad-stack

.PARAMETER SkipClaudeCode
    Don't install the Claude Code CLI or link the filesystem skills.

.PARAMETER SkipClaudeDesktop
    Don't install the Claude Desktop app or write its MCP config.

.PARAMETER NonInteractive
    Don't prompt for a title block / company name (for unattended runs).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\bootstrap.ps1

.EXAMPLE
    # KiCad + kb only, no AI tooling at all:
    powershell -ExecutionPolicy Bypass -File .\bootstrap.ps1 -SkipClaudeCode -SkipClaudeDesktop
#>
[CmdletBinding()]
param(
    [string]$Workspace = (Join-Path $HOME 'kicad-stack'),
    [switch]$SkipClaudeCode,
    [switch]$SkipClaudeDesktop,
    [switch]$NonInteractive
)

$ErrorActionPreference = 'Stop'

# True when at least one Claude surface is being set up. The design-block generator
# deps + the kicad-sch-api MCP are only worth installing if some Claude will use them.
$AnyClaude = -not ($SkipClaudeCode -and $SkipClaudeDesktop)

# ---------------------------------------------------------------------------
# Public tooling repos cloned over HTTPS. (Personal templates & the shared
# design-block library are intentionally NOT here - they stay private. Each
# coworker brings their own title block and design blocks; see the last step.)
# ---------------------------------------------------------------------------
$ToolRepos = @(
    @{ name = 'kicad-bench';            url = 'https://github.com/helicopterrun/kicad-bench.git' },
    @{ name = 'design-block-generator'; url = 'https://github.com/helicopterrun/design-block-generator.git' }
)

# --- pretty output (ASCII only, safe on any Windows console) ---------------
function Info($m) { Write-Host "  -> $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "  [OK] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "  [!] $m"  -ForegroundColor Yellow }
function Step($m) { Write-Host "`n=== $m ===" -ForegroundColor Magenta }
function Die($m)  { Write-Host "  [X] $m" -ForegroundColor Red; exit 1 }
function Have($c) { [bool](Get-Command $c -ErrorAction SilentlyContinue) }

# --- PATH helpers ----------------------------------------------------------
function Update-SessionPath {
    # Re-read PATH from the registry (so winget-installed tools become visible),
    # plus the two per-user bin dirs that don't always land in PATH immediately.
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user    = [Environment]::GetEnvironmentVariable('Path', 'User')
    $extra   = @((Join-Path $HOME '.local\bin'), (Join-Path $env:APPDATA 'npm'))
    $env:Path = (@($machine, $user) + $extra | Where-Object { $_ } ) -join ';'
}

function Add-UserPath($dir) {
    if (-not $dir -or -not (Test-Path $dir)) { return }
    $cur   = [Environment]::GetEnvironmentVariable('Path', 'User')
    $parts = @($cur -split ';' | Where-Object { $_ })
    if ($parts -notcontains $dir) {
        [Environment]::SetEnvironmentVariable('Path', (($parts + $dir) -join ';'), 'User')
        Info "added to your PATH: $dir"
    }
    if (($env:Path -split ';') -notcontains $dir) { $env:Path = "$env:Path;$dir" }
}

# --- tool discovery --------------------------------------------------------
$script:PyExe = $null
function Resolve-Python {
    foreach ($c in 'py', 'python', 'python3') {
        if (Have $c) {
            try { $v = & $c -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null } catch { $v = $null }
            if ($v -match '^\d+\.\d+$') {
                $p = $v.Split('.')
                if ([int]$p[0] -gt 3 -or ([int]$p[0] -eq 3 -and [int]$p[1] -ge 11)) { $script:PyExe = $c; return }
            }
        }
    }
    # Fallback: winget's python.org install doesn't always prepend PATH.
    $glob = Get-ChildItem "$env:LOCALAPPDATA\Programs\Python" -Recurse -Filter python.exe -ErrorAction SilentlyContinue |
            Sort-Object FullName -Descending | Select-Object -First 1
    if ($glob) { Add-UserPath (Split-Path $glob.FullName); $script:PyExe = $glob.FullName }
}

function Find-KicadCli {
    if (Have 'kicad-cli') { return (Get-Command kicad-cli).Source }
    $roots = @('C:\Program Files\KiCad', 'C:\Program Files (x86)\KiCad') | Where-Object { Test-Path $_ }
    foreach ($r in $roots) {
        $hit = Get-ChildItem $r -Recurse -Filter 'kicad-cli.exe' -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($hit) { return $hit.FullName }
    }
    return $null
}

# The kicad-sch-api MCP server executable (installed by `pip install --user kicad-sch-api`).
# Returned as an ABSOLUTE path - Claude Desktop needs absolute commands.
function Find-McpExe {
    foreach ($n in 'kicad-sch-mcp', 'kicad-sch-api-mcp') {
        if (Have $n) { return (Get-Command $n).Source }
    }
    foreach ($n in 'kicad-sch-mcp', 'kicad-sch-api-mcp') {
        $hit = Get-ChildItem (Join-Path $env:APPDATA 'Python') -Recurse -Filter "$n.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($hit) { Add-UserPath (Split-Path $hit.FullName); return $hit.FullName }
    }
    return $null
}

# --- winget install --------------------------------------------------------
function Ensure-Winget {
    if (-not (Have 'winget')) {
        Die "winget not found. Install 'App Installer' from the Microsoft Store, then re-run. (Needs Windows 10 1709+ or Windows 11.)"
    }
}

function Winget-Installed($id) {
    if (-not (Have 'winget')) { return $false }
    $out = winget list --id $id -e --accept-source-agreements 2>$null | Out-String
    return ($out -match [regex]::Escape($id))
}

function Install-Pkg($id, $presentCheck) {
    if (& $presentCheck) { Ok "$id already present - skipping"; return }
    Info "installing $id via winget (a UAC prompt may appear) ..."
    winget install --id $id -e --source winget --accept-package-agreements --accept-source-agreements | Out-Host
    Update-SessionPath
}

# --- link or copy a Claude Code skill folder -------------------------------
function Link-OrCopy($link, $target) {
    if (-not (Test-Path $target)) { Warn "skill source missing (repo not cloned?): $target"; return }
    if (Test-Path $link) {
        $item = Get-Item $link -Force
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            [System.IO.Directory]::Delete($link, $false)  # remove the junction only, NOT its target
        } else {
            Remove-Item $link -Recurse -Force
        }
    }
    try {
        New-Item -ItemType Junction -Path $link -Target $target -ErrorAction Stop | Out-Null
        Ok "linked skill: $(Split-Path $link -Leaf)  (auto-updates on git pull)"
    } catch {
        Copy-Item $target $link -Recurse -Force
        Ok "copied skill: $(Split-Path $link -Leaf)  (re-run this script to refresh)"
    }
}

# --- merge the kicad-sch-api server into a Claude Desktop config file -------
# Preserves any other mcpServers already configured. Claude Desktop's stdio schema
# only requires `command`; the server takes no args so we omit them.
function Write-DesktopMcpConfig($file, $mcpExe) {
    $dir = Split-Path $file -Parent
    New-Item -ItemType Directory -Force -Path $dir | Out-Null

    $cfg = $null
    if (Test-Path $file) {
        $raw = (Get-Content $file -Raw -ErrorAction SilentlyContinue)
        if ($raw -and $raw.Trim()) {
            try { $cfg = $raw | ConvertFrom-Json } catch { Warn "existing $file wasn't valid JSON - backing it up and starting fresh"; Copy-Item $file "$file.bak" -Force }
        }
    }
    if (-not $cfg) { $cfg = [pscustomobject]@{} }
    if (-not $cfg.PSObject.Properties['mcpServers']) {
        $cfg | Add-Member -NotePropertyName 'mcpServers' -NotePropertyValue ([pscustomobject]@{}) -Force
    }
    $cfg.mcpServers | Add-Member -NotePropertyName 'kicad-sch-api' -NotePropertyValue (@{ command = $mcpExe }) -Force

    # Write UTF-8 WITHOUT a BOM - a leading BOM makes JSON.parse (what the Electron app
    # uses) choke. .NET's WriteAllText(path, text) defaults to BOM-less UTF-8.
    $json = $cfg | ConvertTo-Json -Depth 12
    [System.IO.File]::WriteAllText($file, $json)
    Ok "wrote kicad-sch-api MCP -> $file"
}

# ===========================================================================
Write-Host ""
Write-Host "  KiCad PCB stack - Windows bootstrap" -ForegroundColor White
Write-Host "  workspace      : $Workspace"
Write-Host "  Claude Code    : $(if ($SkipClaudeCode)    { 'SKIPPED (-SkipClaudeCode)' }    else { 'included' })"
Write-Host "  Claude Desktop : $(if ($SkipClaudeDesktop) { 'SKIPPED (-SkipClaudeDesktop)' } else { 'included' })"
Write-Host ""

Ensure-Winget

# --- 1. system tools via winget -------------------------------------------
Step "1/8  System tools (winget)"
Install-Pkg 'Git.Git'            { Have 'git' }
Install-Pkg 'Python.Python.3.12' { Resolve-Python; [bool]$script:PyExe }
Install-Pkg 'KiCad.KiCad'        { [bool](Find-KicadCli) }

Update-SessionPath
Resolve-Python
if (-not $script:PyExe) { Die "Python 3.11+ still not found after install. Open a new terminal and re-run, or install Python from python.org." }
Ok "python: $script:PyExe"
if (-not (Have 'git'))  { Die "git still not found after install. Open a new terminal and re-run." }

# --- 2. put kicad-cli on PATH (KiCad's installer does NOT do this) ---------
Step "2/8  Put kicad-cli on PATH"
$cli = Find-KicadCli
if ($cli) { Add-UserPath (Split-Path $cli); Ok "kicad-cli: $cli" }
else      { Warn "kicad-cli.exe not found - check that KiCad installed correctly, then re-run." }

# --- 3. clone / update the public tooling repos ---------------------------
Step "3/8  Clone tooling repos -> $Workspace"
New-Item -ItemType Directory -Force -Path $Workspace | Out-Null
function Sync-Repo($name, $url) {
    $dest = Join-Path $Workspace $name
    if (Test-Path (Join-Path $dest '.git')) {
        Info "updating $name ..."
        git -C $dest pull --ff-only 2>&1 | Out-Host
    } else {
        Info "cloning $name ..."
        git clone $url $dest 2>&1 | Out-Host
        if ($LASTEXITCODE -ne 0) {
            Warn "clone of $name failed - if this repo isn't public yet, ask the maintainer to publish it, then re-run."
        }
    }
}
foreach ($r in $ToolRepos) { Sync-Repo $r.name $r.url }

# --- 4. kicad-bench (kb) via pipx -----------------------------------------
Step "4/8  Install kicad-bench (the 'kb' CLI) via pipx"
& $script:PyExe -m pip install --user --upgrade pip pipx 2>&1 | Out-Host
& $script:PyExe -m pipx ensurepath 2>&1 | Out-Null
Add-UserPath (Join-Path $HOME '.local\bin')
Update-SessionPath

$kbPath = Join-Path $Workspace 'kicad-bench'
if (Have 'kb') {
    Ok "kb already installed (editable install tracks your git pulls)"
} elseif (Test-Path $kbPath) {
    & $script:PyExe -m pipx install --editable $kbPath 2>&1 | Out-Host
} else {
    Warn "kicad-bench repo not present - skipping kb install."
}
# release-prep / bom-assembly / datasheet BOM export read+write .xlsx via openpyxl;
# add it into kb's isolated venv (matches the `xlsx` optional extra).
try { & $script:PyExe -m pipx inject kicad-bench openpyxl 2>&1 | Out-Host } catch { Warn "openpyxl inject skipped (kb not installed yet)" }
Update-SessionPath

# --- 5. Claude apps (Desktop + Code) via winget ---------------------------
Step "5/8  Claude apps"
if (-not $SkipClaudeDesktop) {
    Install-Pkg 'Anthropic.Claude' { Winget-Installed 'Anthropic.Claude' }
    Ok "Claude Desktop installed (or already present)."
} else {
    Info "Claude Desktop - skipped (-SkipClaudeDesktop)"
}
if (-not $SkipClaudeCode) {
    # Native winget package - no Node/npm required. (Older setups used
    # `npm i -g @anthropic-ai/claude-code`; that still works if you prefer it.)
    Install-Pkg 'Anthropic.ClaudeCode' { Have 'claude' }
    Update-SessionPath
    if (Have 'claude') { Ok "Claude Code CLI: $((claude --version 2>$null) | Select-Object -First 1)" }
    else { Warn "claude not on PATH in THIS window yet - it'll be there in a new terminal (or re-run to finish MCP wiring)." }
} else {
    Info "Claude Code - skipped (-SkipClaudeCode)"
}

# --- 6. design-block generator deps + kicad-sch-api MCP -------------------
if ($AnyClaude) {
    Step "6/8  design-block generator deps + kicad-sch-api MCP"
    # kicad-sch-api must be importable by the SAME python that runs generate_block.py,
    # so use --user (a pipx-isolated install would NOT be importable by 'py').
    & $script:PyExe -m pip install --user --upgrade kicad-sch-api jsonschema 2>&1 | Out-Host

    $mcpExe = Find-McpExe
    if ($mcpExe) {
        Ok "kicad-sch-api MCP server: $mcpExe"

        # 6a. Claude Code - register at user scope via the CLI.
        if (-not $SkipClaudeCode -and (Have 'claude')) {
            $existing = (claude mcp list 2>$null) -join "`n"
            if ($existing -match 'kicad-sch-api') { Ok "kicad-sch-api MCP already registered with Claude Code" }
            else { claude mcp add --scope user kicad-sch-api -- "$mcpExe" 2>&1 | Out-Host; Ok "registered kicad-sch-api MCP with Claude Code (user scope)" }
        } elseif (-not $SkipClaudeCode) {
            Warn "claude not on PATH yet - re-run in a new terminal to finish Claude Code MCP registration."
        }

        # 6b. Claude Desktop - write its JSON config (merge, don't clobber).
        if (-not $SkipClaudeDesktop) {
            $desktopConfigs = @( (Join-Path $env:APPDATA 'Claude\claude_desktop_config.json') )
            # MSIX-packaged installs read from a virtualized Roaming dir; write there too if present.
            Get-ChildItem (Join-Path $env:LOCALAPPDATA 'Packages') -Directory -Filter 'Claude_*' -ErrorAction SilentlyContinue | ForEach-Object {
                $pkg = Join-Path $_.FullName 'LocalCache\Roaming\Claude\claude_desktop_config.json'
                if (Test-Path (Split-Path $pkg -Parent)) { $desktopConfigs += $pkg }
            }
            foreach ($f in ($desktopConfigs | Select-Object -Unique)) { Write-DesktopMcpConfig $f $mcpExe }
            Info "In Claude Desktop: Settings > Developer > enable Developer Mode, then FULLY quit and reopen the app."
            Info "The MCP is live when the tools/plug icon shows 'kicad-sch-api' in a chat."
        }
    } else {
        Warn "Could not find the kicad-sch-api MCP server executable."
        Warn "The design-block generator still works (it drives the library directly)."
        Warn "To wire it up later, find the server .exe and:"
        if (-not $SkipClaudeCode)    { Warn "  Claude Code:    claude mcp add --scope user kicad-sch-api -- <path-to-exe>" }
        if (-not $SkipClaudeDesktop) { Warn "  Claude Desktop: add it under mcpServers in %APPDATA%\Claude\claude_desktop_config.json" }
    }
}

# --- 7. wire the Claude skills --------------------------------------------
if ($AnyClaude) {
    Step "7/8  Wire Claude skills"

    # The four SKILL.md skills now live INSIDE the kicad-bench repo, plus the
    # design-block-generator repo root. (Older setups only had two + design-blocks.)
    $skillMap = @(
        @{ link = 'kicad-quality-gate';     target = (Join-Path $Workspace 'kicad-bench\skills\kicad-quality-gate') },
        @{ link = 'kicad-layout-prep';      target = (Join-Path $Workspace 'kicad-bench\skills\kicad-layout-prep') },
        @{ link = 'kicad-product-workflow'; target = (Join-Path $Workspace 'kicad-bench\skills\kicad-product-workflow') },
        @{ link = 'kicad-symbol-style';     target = (Join-Path $Workspace 'kicad-bench\skills\kicad-symbol-style') },
        @{ link = 'design-block-generator'; target = (Join-Path $Workspace 'design-block-generator') }
    )

    # 7a. Claude Code - filesystem skills (linked so a git pull refreshes them).
    if (-not $SkipClaudeCode) {
        $skillsDir = Join-Path $HOME '.claude\skills'
        New-Item -ItemType Directory -Force -Path $skillsDir | Out-Null
        foreach ($s in $skillMap) { Link-OrCopy (Join-Path $skillsDir $s.link) $s.target }
    }

    # 7b. Claude Desktop - can't load filesystem skills, so package uploadable .zips.
    # Only the clean, self-contained SKILL.md folders inside kicad-bench are worth
    # bundling. design-block-generator is a full repo of local Python tooling (it shells
    # out to `kb`/generate_block.py) - that's a Claude Code / terminal skill, not a tidy
    # Desktop upload, so it's excluded here.
    if (-not $SkipClaudeDesktop) {
        $zipDir = Join-Path $Workspace 'claude-desktop-skills'
        New-Item -ItemType Directory -Force -Path $zipDir | Out-Null
        foreach ($s in ($skillMap | Where-Object { $_.link -ne 'design-block-generator' })) {
            if (Test-Path $s.target) {
                $zip = Join-Path $zipDir ("{0}.zip" -f $s.link)
                if (Test-Path $zip) { Remove-Item $zip -Force }
                try { Compress-Archive -Path $s.target -DestinationPath $zip -Force; Ok "packaged skill: $($s.link).zip" }
                catch { Warn "could not zip skill $($s.link): $_" }
            }
        }
        Info "Upload these to Claude Desktop: Settings > Capabilities/Skills > add a skill, pick a .zip from:"
        Info "   $zipDir"
    }
}

# --- 8. your title block + your design blocks (bring your own) ------------
Step "8/8  Your title block + design blocks"

# Personal template dir (yours to fill) - KiCad shows it under User Templates.
$myTpl = Join-Path $Workspace 'my-templates'
New-Item -ItemType Directory -Force -Path $myTpl | Out-Null
[Environment]::SetEnvironmentVariable('KICAD_USER_TEMPLATE_DIR', $myTpl, 'User')
$env:KICAD_USER_TEMPLATE_DIR = $myTpl
Ok "KICAD_USER_TEMPLATE_DIR = $myTpl"
Info "Drop any .kicad project templates in there - they appear under File > New Project from Template > User Templates."

if (-not $NonInteractive) {
    Write-Host ""
    $wks = Read-Host "  Path to YOUR drawing sheet (.kicad_wks) to use, or press Enter to skip"
    if ($wks -and (Test-Path $wks)) {
        Copy-Item $wks (Join-Path $myTpl 'drawing-sheet.kicad_wks') -Force
        Ok "copied your drawing sheet -> $myTpl\drawing-sheet.kicad_wks"
        Info "Apply it per project: File > Page Settings > Drawing sheet file."
    } elseif ($wks) {
        Warn "not found: $wks (skipping)"
    }

    $company = Read-Host "  Company / owner name for your title block, or press Enter to skip"
    if ($company) {
        Set-Content (Join-Path $myTpl 'TITLEBLOCK.txt') @"
Title-block company/owner: $company
Set it per project in KiCad: File > Page Settings > Company = $company
(KiCad has no global title-block setting; enter it once per project, or bake it
 into a template you save in this folder.)
"@
        Ok "noted '$company' in $myTpl\TITLEBLOCK.txt"
        Info "Set it per project: File > Page Settings > Company = $company"
    }
} else {
    Info "(-NonInteractive) skipped title-block prompts. Set yours later via File > Page Settings."
}

# Look for design-block libraries you already have.
Write-Host ""
Info "Looking for design-block libraries you already have ..."
$searchRoots = @(
    (Join-Path $HOME 'Documents\KiCad'),
    (Join-Path $HOME 'Documents'),
    $Workspace
) | Where-Object { Test-Path $_ } | Select-Object -Unique
$foundBlocks = @()
foreach ($root in $searchRoots) {
    $foundBlocks += Get-ChildItem $root -Recurse -Directory -Filter '*.kicad_blocks' -ErrorAction SilentlyContinue |
                    Select-Object -ExpandProperty FullName
}
$foundBlocks = $foundBlocks | Sort-Object -Unique
if ($foundBlocks) {
    Ok "Found design-block libraries:"
    $foundBlocks | ForEach-Object { Write-Host "       $_" }
    Info "Register them in KiCad: Preferences > Manage Design Block Libraries > Add."
} else {
    Info "No existing .kicad_blocks libraries found - that's fine."
    if ($AnyClaude) {
        Info "Make your own with the design-block-generator skill, e.g.:"
        Info '   "Make a design block for an AMS1117 3.3V LDO"'
    }
}
$dbTable = Join-Path $env:APPDATA 'kicad\10.0\design-block-lib-table'
if (Test-Path $dbTable) { Ok "KiCad already has a design-block-lib-table ($dbTable)." }

# --- verify ----------------------------------------------------------------
Step "Verifying"
$cli = Find-KicadCli
if ($cli) { Ok ("kicad-cli " + ((& $cli version 2>$null) | Select-Object -First 1)) } else { Warn "kicad-cli not found" }

if (Have 'kb') {
    Info "kb doctor:"
    kb doctor 2>&1 | Out-Host
} else {
    Warn "kb is not on PATH in THIS window. Open a NEW terminal and run:  kb doctor"
}

if (-not $SkipClaudeCode) {
    if (Have 'claude') { Ok ("claude " + ((claude --version 2>$null) | Select-Object -First 1)) }
    else { Warn "claude not on PATH in this window - open a new terminal." }
}
if (-not $SkipClaudeDesktop) {
    if (Winget-Installed 'Anthropic.Claude') { Ok "Claude Desktop is installed. Enable Developer Mode + restart it to load the MCP." }
    else { Warn "Claude Desktop not detected by winget - re-run, or install it from https://claude.ai/download." }
}

# --- done ------------------------------------------------------------------
Write-Host ""
Write-Host "===================================================================" -ForegroundColor Green
Write-Host " Done. Open a NEW terminal so all PATH changes take effect." -ForegroundColor Green
Write-Host "===================================================================" -ForegroundColor Green
Write-Host ""
Write-Host " Your stack lives in:  $Workspace"
Write-Host ""
Write-Host " First steps:"
Write-Host "   1. Open a new PowerShell window."
Write-Host "   2. Confirm the gate works:        kb doctor"
Write-Host "   3. In KiCad, make a board: File > New Project from Template"
Write-Host "      (KiCad's built-in templates, or your own in $myTpl)."
Write-Host "   4. In the project folder, run the live dashboard:   kb sidecar"
Write-Host "      and the full audit:                              kb audit"
if (-not $SkipClaudeCode) {
    Write-Host "   5. Claude Code:    run 'claude' in a project folder (then /login the first time)."
}
if (-not $SkipClaudeDesktop) {
    Write-Host "   6. Claude Desktop: enable Settings > Developer > Developer Mode, fully quit + reopen,"
    Write-Host "      then upload the skill .zips in $Workspace\claude-desktop-skills."
}
Write-Host ""
Write-Host " Full guide: $Workspace\kicad-bench\windows-onboarding\README.md"
Write-Host ""
