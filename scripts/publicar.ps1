param(
    [string]$Mensaje = "",

    [string]$Rama = ""
)

$ErrorActionPreference = "Stop"

git rev-parse --is-inside-work-tree | Out-Null

$remote = git remote
if (-not $remote) {
    throw "No hay remoto configurado. Agrega origin antes de publicar."
}

if (-not $Mensaje) {
    $Mensaje = "Actualiza proyecto $(Get-Date -Format 'yyyy-MM-dd HH:mm')"
}

if (-not $Rama) {
    $Rama = (git branch --show-current).Trim()
}

git add .

$changes = git diff --cached --name-only
if (-not $changes) {
    throw "No hay cambios para publicar."
}

git commit -m $Mensaje

$hooksPath = (git config --get core.hooksPath)
$autoPushConfigured = $false

if ($hooksPath) {
    $resolvedHooksPath = Join-Path (Get-Location) $hooksPath
    $postCommitHook = Join-Path $resolvedHooksPath "post-commit"
    if (Test-Path $postCommitHook) {
        $autoPushConfigured = $true
    }
}

if (-not $autoPushConfigured) {
    git push origin $Rama
}
