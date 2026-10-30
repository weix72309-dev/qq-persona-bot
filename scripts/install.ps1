<#
Deploy the bot to a target directory and fill in the identifiers.

  .\install.ps1 -Target C:\qq-bot -BotName 小满 -DadQQ 123456 -AdminQQ 123456

Leaves persona.md untouched if the target already has one.
#>
param(
    [Parameter(Mandatory = $true)][string]$Target,
    [Parameter(Mandatory = $true)][string]$BotName,
    [Parameter(Mandatory = $true)][string]$DadQQ,
    [string]$AdminQQ = "",
    [string]$Groups = "",
    [string]$DadAlias = "爸爸",
    [string]$DeepSeekKey = "",
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$asset = Join-Path $PSScriptRoot "..\assets\qq-bot"
if (-not (Test-Path $asset)) { throw "assets not found: $asset" }

New-Item -ItemType Directory -Force -Path $Target | Out-Null
$Target = (Resolve-Path $Target).Path

Get-ChildItem -LiteralPath $asset -Force | ForEach-Object {
    $dest = Join-Path $Target $_.Name
    if ($_.Name -eq "persona.md" -and (Test-Path $dest) -and -not $Force) {
        Write-Host "keep existing persona.md"
        return
    }
    if ($_.Name -eq "config.json" -and (Test-Path $dest) -and -not $Force) {
        Write-Host "keep existing config.json (use -Force to overwrite)"
        return
    }
    Copy-Item -LiteralPath $_.FullName -Destination $dest -Recurse -Force
}

$cfgPath = Join-Path $Target "config.json"
$cfg = Get-Content -LiteralPath $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json
$cfg.bot_name = $BotName
$cfg.dad_qq = @([int64]$DadQQ)
if ($AdminQQ) { $cfg.admin_qq = @([int64]$AdminQQ) }
$cfg.persona_vars.name = $BotName
$cfg.persona_vars.'名字' = $BotName
$cfg.persona_vars.dad = $DadAlias
$cfg.persona_vars.'爸爸的群昵称' = $DadAlias
$cfg.behavior.wake_words = @($BotName)
if ($Groups) { $cfg.behavior.groups = @($Groups -split "," | ForEach-Object { $_.Trim() } | Where-Object { $_ }) }
$cfg | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $cfgPath -Encoding UTF8

if ($DeepSeekKey) {
    "DEEPSEEK_API_KEY=$DeepSeekKey" | Set-Content -LiteralPath (Join-Path $Target ".env") -Encoding UTF8
    Write-Host "wrote .env"
} elseif (-not (Test-Path (Join-Path $Target ".env"))) {
    Copy-Item -LiteralPath (Join-Path $Target ".env.example") -Destination (Join-Path $Target ".env")
    Write-Host "created .env from example - fill in DEEPSEEK_API_KEY"
}

Push-Location $Target
try {
    python -m bot.selftest
    if ($LASTEXITCODE -ne 0) { throw "selftest failed" }
    Write-Host ""
    Write-Host "deployed to $Target"
    Write-Host "next: python -m bot.main --check"
} finally {
    Pop-Location
}
