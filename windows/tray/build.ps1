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

$render = Join-Path $here "render_icons.py"
$rendered = $false
foreach ($candidate in @("python", "python3", "py")) {
    if (Get-Command $candidate -ErrorAction SilentlyContinue) {
        & $candidate $render $out
        if ($LASTEXITCODE -ne 0) { throw "icon render failed" }
        $rendered = $true
        break
    }
}
if (-not $rendered) {
    $wslRender = Convert-ToWslPath $render
    $wslOut = Convert-ToWslPath $out
    & wsl.exe -e python3 $wslRender $wslOut
    if ($LASTEXITCODE -ne 0) {
        & wsl.exe -e bash -lc "python3 '$wslRender' '$wslOut'"
        if ($LASTEXITCODE -ne 0) { throw "icon render failed" }
    }
}

$csc = Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if (-not (Test-Path $csc)) { throw "csc.exe not found" }
$fx = Split-Path $csc
& $csc /nologo /target:winexe /utf8output /codepage:65001 `
    /reference:"$fx\System.Windows.Forms.dll" `
    /reference:"$fx\System.Drawing.dll" `
    /win32icon:"$out\idle.ico" `
    /resource:"$out\idle.ico",idle.ico `
    /resource:"$out\recording.ico",recording.ico `
    /resource:"$out\stopped.ico",stopped.ico `
    /resource:"$out\busy.ico",busy.ico `
    /out:"$out\LiveRecorderTray.exe" `
    (Join-Path $here "TrayApp.cs")
if ($LASTEXITCODE -ne 0) { throw "csc failed" }
Write-Output "built $out\LiveRecorderTray.exe"
