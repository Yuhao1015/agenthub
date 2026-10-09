# Fixed local chooser. ASCII source also works with Windows PowerShell 5.1.
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type -Path (Join-Path $PSScriptRoot 'workdir-dialog.cs')
$owner = New-Object System.Windows.Forms.Form
try {
    $owner.ShowInTaskbar = $false
    $owner.TopMost = $true
    $owner.Opacity = 0
    $owner.StartPosition = [System.Windows.Forms.FormStartPosition]::CenterScreen
    $owner.Size = New-Object System.Drawing.Size(1, 1)
    $owner.Show()
    $owner.Activate()
    $title = [regex]::Unescape('\u9009\u62e9 AgentHub \u7684\u5de5\u4f5c\u6587\u4ef6\u5939')
    $path = [AgentHub.FolderPicker]::Pick($owner.Handle, $title)
    if ($null -ne $path) {
        @{ path = $path } | ConvertTo-Json -Compress
    } else {
        @{ cancelled = $true } | ConvertTo-Json -Compress
    }
} finally {
    $owner.Dispose()
}
