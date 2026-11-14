param(
    [string] $QtPrefix
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$publish = Join-Path $here "build"
# MinGW cannot write object files on a UNC path. Building from \\wsl.localhost\...
# keeps sources there and puts the CMake work directory on a local drive.
$onUnc = $here.StartsWith("\\")
if ($onUnc) {
    $cache = Join-Path $env:LOCALAPPDATA "live-recorder\tray"
} else {
    $cache = Join-Path $here ".cache"
}
$out = if ($onUnc) { Join-Path $cache "stage" } else { $publish }
New-Item -ItemType Directory -Force -Path $cache | Out-Null

function Convert-ToWslPath([string] $path) {
    if ($path -match '^\\\\wsl(\.localhost|\$)\\([^\\]+)\\(.*)$') {
        return "/" + ($Matches[3] -replace '\\', '/')
    }
    $converted = & wsl.exe wslpath -u -- $path
    if ($LASTEXITCODE -ne 0) { throw "wslpath failed: $path" }
    return ($converted | Out-String).Trim()
}

function Assert-QtPrefix([string] $dir) {
    if (-not $dir) { throw "Qt 6 path is empty" }
    $cfg = Join-Path $dir "lib\cmake\Qt6\Qt6Config.cmake"
    if (-not (Test-Path $cfg)) {
        throw "Qt 6 was not found at $dir (missing lib\cmake\Qt6\Qt6Config.cmake). Install the Widgets kit or pass -QtPrefix."
    }
    return (Resolve-Path $dir).Path
}

function Get-QtRank([string] $path) {
    $version = 0
    if ($path -match '\\(\d+)\.(\d+)\.(\d+)\\') {
        $version = [int]$Matches[1] * 1000000 + [int]$Matches[2] * 1000 + [int]$Matches[3]
    }
    $leaf = Split-Path $path -Leaf
    $kit = 0
    if ($leaf -match 'arm64') { $kit = 0 }
    elseif ($leaf -match 'msvc' -and $leaf -match '_64$') { $kit = 2 }
    elseif ($leaf -match 'mingw' -and $leaf -match '_64$') { $kit = 1 }
    return $version * 10 + $kit
}

function Find-InstalledQt {
    foreach ($name in @("qtpaths6.exe", "qtpaths.exe")) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        $queried = & $cmd.Source --query QT_INSTALL_PREFIX
        if ($LASTEXITCODE -ne 0) { continue }
        $text = ($queried | Out-String).Trim()
        $cfg = Join-Path $text "lib\cmake\Qt6\Qt6Config.cmake"
        if (Test-Path $cfg) { return (Resolve-Path $text).Path }
    }

    $found = New-Object System.Collections.Generic.List[string]
    foreach ($root in @("C:\Qt", (Join-Path $env:LOCALAPPDATA "Qt"))) {
        if (-not (Test-Path $root)) { continue }
        foreach ($versionDir in (Get-ChildItem $root -Directory -ErrorAction SilentlyContinue)) {
            foreach ($kitDir in (Get-ChildItem $versionDir.FullName -Directory -ErrorAction SilentlyContinue)) {
                $cfg = Join-Path $kitDir.FullName "lib\cmake\Qt6\Qt6Config.cmake"
                if (Test-Path $cfg) { $found.Add($kitDir.FullName) }
            }
        }
    }
    if ($found.Count -eq 0) {
        throw "Qt 6 is not installed. Install Qt 6 with Widgets and pass -QtPrefix, or set QT_PREFIX."
    }
    return ($found | Sort-Object { Get-QtRank $_ } -Descending | Select-Object -First 1)
}

function Find-CMake {
    $cmd = Get-Command cmake -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $tools = "C:\Qt\Tools"
    if (Test-Path $tools) {
        $found = Get-ChildItem $tools -Directory -Filter "CMake*" | Sort-Object Name -Descending | Select-Object -First 1
        if ($found) {
            $exe = Join-Path $found.FullName "bin\cmake.exe"
            if (Test-Path $exe) { return $exe }
        }
    }
    throw "cmake is not installed. Install CMake 3.21 or newer, or install it from the Qt installer."
}

function Find-ToolBin([string] $filter, [string] $exeName) {
    $cmd = Get-Command $exeName -ErrorAction SilentlyContinue
    if ($cmd) { return Split-Path $cmd.Source -Parent }
    $tools = "C:\Qt\Tools"
    if (-not (Test-Path $tools)) { return $null }
    $found = Get-ChildItem $tools -Directory -Filter $filter | Sort-Object Name -Descending | Select-Object -First 1
    if (-not $found) { return $null }
    $exe = Join-Path $found.FullName $exeName
    if (Test-Path $exe) { return $found.FullName }
    $nested = Join-Path $found.FullName "bin\$exeName"
    if (Test-Path $nested) { return (Join-Path $found.FullName "bin") }
    return $null
}

$icons = Join-Path $cache "icons"
New-Item -ItemType Directory -Force -Path $icons | Out-Null
$render = Join-Path $here "render_icons.py"
$rendered = $false
foreach ($candidate in @("py", "python", "python3")) {
    $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
    if (-not $cmd) { continue }
    if ($cmd.Source -like "*\WindowsApps\*") { continue }
    $pyArgs = @()
    if ($candidate -eq "py") { $pyArgs = @("-3") }
    & $cmd.Source @pyArgs $render $icons
    if ($LASTEXITCODE -eq 0) {
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

if ($QtPrefix) {
    $prefix = Assert-QtPrefix $QtPrefix
} elseif ($env:QT_PREFIX) {
    $prefix = Assert-QtPrefix $env:QT_PREFIX
} else {
    $prefix = Find-InstalledQt
}

$cmake = Find-CMake
$work = Join-Path $cache "cmake"
$leaf = Split-Path $prefix -Leaf
$generator = "Visual Studio 17 2022"
if ($leaf -match 'mingw') { $generator = "Ninja" }
elseif ($leaf -match 'msvc2019') { $generator = "Visual Studio 16 2019" }
$stampPath = Join-Path $cache "generator.txt"
if ((Test-Path $stampPath) -and (Test-Path $work)) {
    $previous = (Get-Content $stampPath -Raw).Trim()
    if ($previous -ne $generator) { Remove-Item $work -Recurse -Force }
}
if ($leaf -match 'mingw') {
    $mingwBin = Find-ToolBin "mingw*_64" "g++.exe"
    if (-not $mingwBin) { throw "MinGW for this Qt kit was not found under C:\Qt\Tools" }
    $ninjaBin = Find-ToolBin "Ninja*" "ninja.exe"
    if (-not $ninjaBin) { throw "Ninja was not found. Install Ninja or add it from the Qt installer." }
    $env:PATH = "$mingwBin;$ninjaBin;$prefix\bin;$env:PATH"
    & $cmake -S $here -B $work -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_CXX_COMPILER=g++ "-DCMAKE_PREFIX_PATH=$prefix" "-DTRAY_ICON_DIR=$icons"
} elseif ($leaf -match 'msvc2019') {
    & $cmake -S $here -B $work -G "Visual Studio 16 2019" -A x64 "-DCMAKE_PREFIX_PATH=$prefix" "-DTRAY_ICON_DIR=$icons"
} else {
    & $cmake -S $here -B $work -G "Visual Studio 17 2022" -A x64 "-DCMAKE_PREFIX_PATH=$prefix" "-DTRAY_ICON_DIR=$icons"
}
if ($LASTEXITCODE -ne 0) { throw "cmake configure failed" }
Set-Content -Path $stampPath -Value $generator -Encoding ascii

& $cmake --build $work --config Release --parallel
if ($LASTEXITCODE -ne 0) { throw "cmake build failed" }

$built = Join-Path $work "Release\LiveRecorderTray.exe"
if (-not (Test-Path $built)) {
    $built = Join-Path $work "LiveRecorderTray.exe"
}
if (-not (Test-Path $built)) { throw "build output not found" }

# windows\tray\tray.json is the local config. The publish folder is replaced
# below, so keep an existing build\tray.json when the source file is absent.
$traySource = Join-Path $here "tray.json"
$trayPublish = Join-Path $publish "tray.json"
$trayHeld = $null
if (Test-Path $traySource) {
    $trayHeld = $traySource
} elseif (Test-Path $trayPublish) {
    $trayHeld = Join-Path $cache "tray.json"
    Copy-Item $trayPublish $trayHeld -Force
}

if (Test-Path $out) { Remove-Item $out -Recurse -Force }
New-Item -ItemType Directory -Force -Path $out | Out-Null
Copy-Item $built (Join-Path $out "LiveRecorderTray.exe")

$deploy = Join-Path $prefix "bin\windeployqt.exe"
if (-not (Test-Path $deploy)) { $deploy = Join-Path $prefix "bin\windeployqt6.exe" }
if (-not (Test-Path $deploy)) { throw "windeployqt was not found in $prefix\bin" }
& $deploy --release --compiler-runtime --dir $out (Join-Path $out "LiveRecorderTray.exe")
if ($LASTEXITCODE -ne 0) { throw "windeployqt failed" }

$imageFormats = Join-Path $prefix "plugins\imageformats"
if (Test-Path $imageFormats) {
    $dest = Join-Path $out "imageformats"
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    foreach ($name in @("qico.dll", "qpng.dll")) {
        $src = Join-Path $imageFormats $name
        if (Test-Path $src) { Copy-Item $src (Join-Path $dest $name) -Force }
    }
}

if ($onUnc) {
    if (Test-Path $publish) { Remove-Item $publish -Recurse -Force }
    Copy-Item $out $publish -Recurse -Force
}
if ($trayHeld) {
    Copy-Item $trayHeld (Join-Path $publish "tray.json") -Force
}
Write-Output "built $publish\LiveRecorderTray.exe"
