[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$LogPath
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($LogPath)) {
    $LogPath = Join-Path $PSScriptRoot "log"
}

$pngFiles = @(Get-ChildItem -LiteralPath $LogPath -Filter "*.png" -File)

if ($pngFiles.Count -eq 0) {
    Write-Host "[INFO] No PNG files found in: $LogPath"
    exit 0
}

foreach ($pngFile in $pngFiles) {
    if ($PSCmdlet.ShouldProcess($pngFile.FullName, "Delete log PNG file")) {
        Remove-Item -LiteralPath $pngFile.FullName -Force
        Write-Host "[DELETED] $($pngFile.Name)"
    }
}

Write-Host "[INFO] Processed $($pngFiles.Count) log PNG file(s)."