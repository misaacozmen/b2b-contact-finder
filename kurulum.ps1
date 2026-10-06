# Kurulum (Talimat 30): proje Python ortamini, paketleri, tarayiciyi ve masaustu kisayolunu kurar.
#
# Kullanim:  KURULUM.cmd dosyasina cift tiklayin
#     ya da: powershell -NoProfile -ExecutionPolicy Bypass -File kurulum.ps1 [-KisayolYok]
#
# Ortam proje klasorunun icine kurulur (.runtime). Bilgisayardaki baska Python
# kurulumlarina dokunulmaz. Komut tekrar calistirilabilir; kurulu olanlari atlar.

param([switch]$KisayolYok)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$runtime = Join-Path $root '.runtime'
$prefix = Join-Path $runtime 'python3147-sqlite3534'
$python = Join-Path $prefix 'python.exe'
$pythonw = Join-Path $prefix 'pythonw.exe'
$micromamba = Join-Path $runtime 'micromamba.exe'
$mambaRoot = Join-Path $runtime 'mamba'
$micromambaUrl = 'https://github.com/mamba-org/micromamba-releases/releases/latest/download/micromamba-win-64'

function Step($text) { Write-Host "`n== $text" -ForegroundColor Cyan }
function Check-Exit($what) { if ($LASTEXITCODE -ne 0) { throw "$what basarisiz (cikis kodu $LASTEXITCODE)." } }

function Test-SafeSqlite {
    if (-not (Test-Path $python)) { return $false }
    & $python -c "import sqlite3, sys; v = tuple(int(p) for p in sqlite3.sqlite_version.split('.')[:3]); sys.exit(0 if v >= (3, 51, 3) else 1)" 2>$null
    return ($LASTEXITCODE -eq 0)
}

Step "Proje klasoru: $root"
New-Item -ItemType Directory -Force -Path $runtime | Out-Null

if (Test-SafeSqlite) {
    Step 'Python ortami zaten kurulu, atlaniyor.'
} else {
    if (-not (Test-Path $micromamba)) {
        Step 'micromamba indiriliyor (conda-forge paket yoneticisi, yaklasik 15 MB)'
        Invoke-WebRequest -Uri $micromambaUrl -OutFile $micromamba -UseBasicParsing
    }
    Step 'Python 3.14 ve SQLite (en az 3.51.3) kuruluyor (birkac dakika surer)'
    & $micromamba create --yes --root-prefix $mambaRoot --prefix $prefix --channel conda-forge --override-channels 'python=3.14' 'sqlite>=3.51.3' pip tk
    Check-Exit 'Python ortami kurulumu'
    if (-not (Test-SafeSqlite)) { throw 'Kurulan Python icindeki SQLite 3.51.3 surumunden eski.' }
}

Step 'Python paketleri kuruluyor'
& $python -m pip install --disable-pip-version-check --no-warn-script-location -r (Join-Path $root 'requirements-browser.txt') -r (Join-Path $root 'requirements-dev.txt')
Check-Exit 'Paket kurulumu'

Step 'Chromium tarayicisi kuruluyor (JavaScript ile acilan sayfalar icin)'
& $python -m playwright install chromium
Check-Exit 'Tarayici kurulumu'

Step 'Panel denetimi'
Push-Location $root
try {
    $check = & $python (Join-Path $root 'tools\kosu_paneli.py') --self-check
    Check-Exit 'Panel denetimi'
} finally {
    Pop-Location
}
if (($check | Select-Object -Last 1) -ne 'PANEL_OK') { throw "Panel denetimi beklenmeyen cikti verdi: $check" }
Write-Host 'PANEL_OK'

if (-not $KisayolYok) {
    Step 'Masaustu kisayolu olusturuluyor'
    $desktop = [Environment]::GetFolderPath('Desktop')
    $name = "Fuar Ko$([char]0x015F)u Paneli.lnk"
    $shell = New-Object -ComObject WScript.Shell
    $link = $shell.CreateShortcut((Join-Path $desktop $name))
    $link.TargetPath = $pythonw
    $link.Arguments = '"' + (Join-Path $root 'tools\kosu_paneli.py') + '"'
    $link.WorkingDirectory = $root
    $link.Description = 'Fuar kosu paneli'
    $link.Save()
    Write-Host "Kisayol: $(Join-Path $desktop $name)"
}

Step 'Kurulum tamamlandi.'
Write-Host 'Sonraki adimlar:'
Write-Host '  1. Masaustundeki "Fuar Kosu Paneli" kisayolunu acin. Ucretsiz kosu icin API anahtari gerekmez.'
Write-Host '  2. Ucretli kosu kullanacaksaniz anahtarlari bir kez kaydedin:'
Write-Host "     `"$python`" tools\api_anahtarlari.py"
Write-Host '  3. Ayrintilar: README.md ve docs\KULLANIM_KILAVUZU.md'
