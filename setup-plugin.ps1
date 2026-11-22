$ErrorActionPreference = 'Stop'
$studio = Join-Path $PSScriptRoot 'studio.cmd'
& $studio setup-plugin
if ($LASTEXITCODE -ne 0) {
    throw "Failed to generate plugin mcp_config.json."
}
