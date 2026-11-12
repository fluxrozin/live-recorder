$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$out = Join-Path $here "build"
New-Item -ItemType Directory -Force -Path $out | Out-Null

function Convert-ToWslPath([string] $path) {
    if ($path -match '^\\\\wsl(\.localhost|\$)\\([^\\]+)\\(.*)$') {
        return "/" + ($Matches[3] -replace '\\', '/')
    }
    $converted = & wsl.exe wslpath -u -- $path
    if ($LASTEXITCODE -ne 0) { throw "wslpath failed: $path" }
    return ($converted | Out-String).Trim()
}

$icons = Join-Path $out "icons"
New-Item -ItemType Directory -Force -Path $icons | Out-Null
$render = Join-Path $here "render_icons.py"
$rendered = $false
foreach ($candidate in @("python", "python3", "py")) {
    if (Get-Command $candidate -ErrorAction SilentlyContinue) {
        & $candidate $render $icons
        if ($LASTEXITCODE -ne 0) { throw "icon render failed" }
        $rendered = $true
        break
    }
}
if (-not $rendered) {
    $wslRender = Convert-ToWslPath $render
    $wslIcons = Convert-ToWslPath $icons
    & wsl.exe -e python3 $wslRender $wslIcons
    if ($LASTEXITCODE -ne 0) {
        & wsl.exe -e bash -lc "python3 '$wslRender' '$wslIcons'"
        if ($LASTEXITCODE -ne 0) { throw "icon render failed" }
    }
}

if (-not (Get-Command dotnet -ErrorAction SilentlyContinue)) {
    throw ".NET 10 SDK is not installed. Install it from https://dotnet.microsoft.com/download/dotnet/10.0"
}
Push-Location $here
try {
    & dotnet publish ".\LiveRecorderTray.csproj" `
        -c Release `
        -r win-x64 `
        --self-contained true `
        -p:PublishSingleFile=true `
        -p:IncludeNativeLibrariesForSelfExtract=true `
        -p:PublishTrimmed=false `
        -p:DebugType=none `
        -p:DebugSymbols=false `
        -o $out
    if ($LASTEXITCODE -ne 0) { throw "dotnet publish failed" }
}
finally {
    Pop-Location
}
Write-Output "built $out\LiveRecorderTray.exe"
