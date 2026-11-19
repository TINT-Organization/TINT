# Prepara en el servidor (Windows) el clon aislado del banco de medición.
# Lo copia y ejecuta bench/run.py (subcomando prepare); no lo corras a mano.
#   powershell -NoProfile -ExecutionPolicy Bypass -File setup.ps1 -Commit <sha> -Secret <JWT_SECRET sintético>
param(
  [Parameter(Mandatory)][string]$Commit,
  [Parameter(Mandatory)][string]$Secret
)
$ErrorActionPreference = 'Stop'
$bench  = 'C:\tint\bench'
$repo   = "$bench\tint-monorepo"
$public = 'C:\tint\tint-monorepo'

if (-not (Test-Path $repo)) { git clone --quiet $public $repo }
Set-Location $repo
git fetch --quiet origin
git checkout --quiet --force $Commit
if ($LASTEXITCODE -ne 0) { throw "no se pudo fijar $Commit" }

# .env del banco: parte del público (mismas variables) y reemplaza solo lo necesario.
$overrides = [ordered]@{
  'JWT_SECRET'                  = $Secret
  'FIREBASE_PROJECT_ID'         = 'demo-tint'
  'FIREBASE_AUTH_EMULATOR_HOST' = 'firebase-auth-emulator:9099'
  'COMPOSE_PROJECT_NAME'        = 'tintbench'
  'COMPOSE_FILE'                = "docker-compose.yml;$bench\compose.bench.yml"
}
$pattern = '^\s*(' + ($overrides.Keys -join '|') + ')='
$lines = @(Get-Content "$public\.env" | Where-Object { $_ -notmatch $pattern })
foreach ($key in $overrides.Keys) { $lines += "$key=$($overrides[$key])" }
Set-Content -Path "$repo\.env" -Value $lines -Encoding ascii
Write-Output "listo: $(git rev-parse --short HEAD)"
