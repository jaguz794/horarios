param(
    [Parameter(Mandatory = $true)]
    [string]$Mensaje,

    [string]$Rama = "master"
)

$ErrorActionPreference = "Stop"

git rev-parse --is-inside-work-tree | Out-Null

$remote = git remote
if (-not $remote) {
    throw "No hay remoto configurado. Agrega origin antes de publicar."
}

git add .

$changes = git diff --cached --name-only
if (-not $changes) {
    throw "No hay cambios para publicar."
}

git commit -m $Mensaje
git push origin $Rama
