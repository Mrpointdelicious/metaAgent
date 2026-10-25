<#
创建日期：2026-10-22
文件功能：按目标镜像中的发布清单切换 Prompt bundle，支持列出版本及失败恢复。
#>
[CmdletBinding(DefaultParameterSetName = 'Switch')]
param(
    [Parameter(Mandatory = $true, ParameterSetName = 'Switch')]
    [ValidatePattern('^[a-z][a-z0-9_-]{0,63}$')]
    [string]$Bundle,

    [Parameter(Mandatory = $true, ParameterSetName = 'List')]
    [switch]$List,

    [string]$Container = 'meta_agent',

    [ValidateRange(10, 600)]
    [int]$TimeoutSeconds = 120
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$helperPath = Join-Path $PSScriptRoot 'prompt_switch.py'
$runtimePath = Join-Path $repoRoot '.venv\Scripts\python.exe'
$helperArguments = @('--container', $Container, '--timeout', $TimeoutSeconds.ToString())
if ($List) {
    $helperArguments += '--list'
} else {
    $helperArguments += @('--bundle', $Bundle)
}

Push-Location -LiteralPath $repoRoot
try {
    if (Test-Path -LiteralPath $runtimePath) {
        & $runtimePath -X utf8 $helperPath @helperArguments
    } elseif (Get-Command uv -ErrorAction SilentlyContinue) {
        & uv run --project $repoRoot python -X utf8 $helperPath @helperArguments
    } else {
        throw 'Python runtime missing. Run uv sync for this project first.'
    }
    $resultCode = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $resultCode
