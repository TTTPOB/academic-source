# Install this checkout, never an upstream package release.
$ErrorActionPreference = "Stop"
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "Install uv first: https://docs.astral.sh/uv/"
}
Push-Location $PSScriptRoot
try {
    & uv sync --frozen --extra vpnsci --extra fast
    if ($LASTEXITCODE -ne 0) { throw "uv sync failed" }
    Write-Host "Ready: uv run academic-source serve --host 127.0.0.1 --port 8000"
    Write-Host "MCP stdio: uv run academic-source mcp"
} finally {
    Pop-Location
}
