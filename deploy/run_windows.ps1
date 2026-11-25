# Run the cloud dashboard, optionally connected to an Orchestration service.
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $repoRoot
# Configure ORCHESTRATION_URL and ORCHESTRATION_CONTROL_TOKEN in the process
# environment when local models are wanted. Orchestration owns the controller.
& (Join-Path $repoRoot '.venv\Scripts\python.exe') -m llm_gateway
exit $LASTEXITCODE
