[CmdletBinding()]
param([switch]$Execute, [switch]$VerifyProtected, [switch]$SafetyFunctionsOnly)

$ErrorActionPreference = 'Stop'

function Assert-PruneRoot([string]$Path) {
    $item = Get-Item -LiteralPath $Path -Force
    if (-not $item.PSIsContainer) { throw "prune root is not a directory: $Path" }
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "reparse point at prune root: $Path"
    }
    (Resolve-Path -LiteralPath $item.FullName).Path
}

function Assert-PruneTarget([string]$Path, [string]$Parent, [string[]]$ProtectedPaths, [string[]]$WorktreePaths) {
    $resolved = Assert-PruneRoot $Path
    $parentPath = [IO.Path]::GetFullPath($Parent).TrimEnd([char[]]'\/')
    if ((Split-Path -Parent $resolved) -ne $parentPath) {
        throw "candidate is not a direct Documents child: $resolved"
    }
    $comparison = [StringComparison]::OrdinalIgnoreCase
    $separator = [IO.Path]::DirectorySeparatorChar
    foreach ($guarded in (@($ProtectedPaths) + @($WorktreePaths))) {
        if ([string]::IsNullOrWhiteSpace($guarded)) { continue }
        $guardedPath = [IO.Path]::GetFullPath($guarded).TrimEnd([char[]]'\/')
        if ([string]::Equals($resolved, $guardedPath, $comparison) -or
            $resolved.StartsWith($guardedPath + $separator, $comparison) -or
            $guardedPath.StartsWith($resolved + $separator, $comparison)) {
            throw "protected/worktree overlap: $resolved and $guardedPath"
        }
    }
    $resolved
}

# Loading only these pure guards never enumerates or deletes incident data.
if ($SafetyFunctionsOnly) { return }

$workspaceRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$documents = (Resolve-Path (Join-Path $workspaceRoot '..')).Path
$protected = @(
    (Join-Path $documents 'Python b2b'),
    (Join-Path $documents 'Python b2b_incident_20260827_893'),
    (Join-Path $documents 'Python b2b_recovery_final10_r2')
)
$allowed = '^(Python b2b_disposable_.*|Python b2b_recovery_(fast|final(?:3|4|5|6|7|8|9|10))$)'
$protectedResolved = $protected | ForEach-Object { (Resolve-Path -LiteralPath $_).Path }

function Get-TreeState([string]$Root, [switch]$HashFiles, [switch]$AllowReparse, [string]$ExcludeRelative) {
    $rootPath = Assert-PruneRoot $Root
    $stack = [System.Collections.Generic.Stack[string]]::new()
    $stack.Push($rootPath)
    $files = [System.Collections.Generic.List[object]]::new()
    while ($stack.Count -gt 0) {
            $current = $stack.Pop()
        foreach ($item in @(Get-ChildItem -LiteralPath $current -Force)) {
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                if (-not $AllowReparse) { throw "reparse point encountered: $($item.FullName)" }
                $target = @($item.Target) -join '|'
                $files.Add([pscustomobject]@{ Relative = $item.FullName.Substring($rootPath.Length + 1); Hash = "REPARSE:$($item.LinkType):$target"; Bytes = [int64]0 })
                continue
            }
            if ($item.PSIsContainer) { $stack.Push($item.FullName) }
            else {
                $relative = $item.FullName.Substring($rootPath.Length + 1)
                if ($relative -eq $ExcludeRelative) { continue }
                $hash = $null
                if ($HashFiles) { $hash = (Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256).Hash.ToLowerInvariant() }
                $files.Add([pscustomobject]@{ Relative = $relative; Hash = $hash; Bytes = [int64]$item.Length })
            }
        }
    }
    $digest = $null
    if ($HashFiles) {
        $material = ($files | Sort-Object Relative | ForEach-Object { "$($_.Relative):$($_.Hash)`n" }) -join ''
        $sha = [Security.Cryptography.SHA256]::Create()
        try { $digest = ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($material))).Replace('-', '')).ToLowerInvariant() }
        finally { $sha.Dispose() }
    }
    [pscustomobject]@{ Path = $rootPath; Sha256 = $digest; Bytes = ($files | Measure-Object Bytes -Sum).Sum; Files = $files.Count }
}

function Get-WorktreePaths([string]$Repo) {
    $lines = @(git -C $Repo worktree list --porcelain)
    if ($LASTEXITCODE -ne 0) { throw 'git worktree list failed' }
    @($lines | Where-Object { $_ -match '^worktree (.+)$' } | ForEach-Object { (Resolve-Path -LiteralPath $Matches[1]).Path })
}

if ($VerifyProtected) {
    $receiptPath = Join-Path $workspaceRoot 'recovery_validation\prune_receipt.json'
    $receipt = Get-Content -Raw -LiteralPath $receiptPath | ConvertFrom-Json
    $protectedAfter = @($protectedResolved | ForEach-Object { Get-TreeState $_ -HashFiles -AllowReparse -ExcludeRelative 'recovery_validation\prune_receipt.json' })
    for ($i = 0; $i -lt $protectedAfter.Count; $i++) {
        if ($receipt.protected_before[$i].Sha256 -ne $protectedAfter[$i].Sha256) { throw "protected hash changed: $($protectedAfter[$i].Path)" }
    }
    [pscustomobject]@{ protected_unchanged = $true; protected_after = $protectedAfter } | ConvertTo-Json -Depth 8
    exit 0
}

$children = @(Get-ChildItem -LiteralPath $documents -Force | Where-Object { $_.PSIsContainer -and $_.Name -match $allowed })
if ($children.Count -ne 22) { throw "expected exactly 22 disposable candidates, found $($children.Count)" }
$worktrees = @(Get-WorktreePaths $protected[0])
$targets = foreach ($child in $children) {
    $resolved = Assert-PruneTarget $child.FullName $documents $protectedResolved $worktrees
    Get-TreeState $resolved
}
$totalBytes = [int64](($targets | Measure-Object Bytes -Sum).Sum)
$summary = [pscustomobject]@{ mode = $(if ($Execute) { 'execute' } else { 'dry-run' }); candidates = $targets; total_bytes = $totalBytes; total_gib = ($totalBytes / 1GB) }
if (-not $Execute) { $summary | ConvertTo-Json -Depth 6; exit 0 }

$protectedBefore = @($protectedResolved | ForEach-Object { Get-TreeState $_ -HashFiles -AllowReparse })
$receipt = [pscustomobject]@{
    created_at = [DateTime]::UtcNow.ToString('o')
    targets = $targets
    protected_before = $protectedBefore
    candidate_count = $targets.Count
    total_bytes = $totalBytes
}
$receiptPath = Join-Path $workspaceRoot 'recovery_validation\prune_receipt.json'
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $receiptPath) | Out-Null
$receipt | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $receiptPath -Encoding UTF8
foreach ($target in $targets) {
    $currentWorktrees = @(Get-WorktreePaths $protected[0])
    $safeTarget = Assert-PruneTarget $target.Path $documents $protectedResolved $currentWorktrees
    Remove-Item -LiteralPath $safeTarget -Recurse -Force
}
$protectedAfter = @($protectedResolved | ForEach-Object { Get-TreeState $_ -HashFiles -AllowReparse -ExcludeRelative 'recovery_validation\prune_receipt.json' })
for ($i = 0; $i -lt $protectedBefore.Count; $i++) { if ($protectedBefore[$i].Sha256 -ne $protectedAfter[$i].Sha256) { throw "protected hash changed: $($protectedBefore[$i].Path)" } }
if ($totalBytes -lt 19GB) { throw "less than 19 GiB was freed: $totalBytes bytes" }
$receipt | Add-Member -NotePropertyName protected_after -NotePropertyValue $protectedAfter
$receipt | Add-Member -NotePropertyName deleted_at -NotePropertyValue ([DateTime]::UtcNow.ToString('o'))
$receipt | Add-Member -NotePropertyName freed_bytes -NotePropertyValue $totalBytes
$receipt | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $receiptPath -Encoding UTF8
$receipt | ConvertTo-Json -Depth 8
