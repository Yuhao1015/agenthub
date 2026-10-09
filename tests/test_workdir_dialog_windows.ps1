# Compile and configure the real Windows dialog without opening a window.
$ErrorActionPreference = 'Stop'
$source = Join-Path $PSScriptRoot '../assets/workdir-dialog.cs'
if (-not (Test-Path -LiteralPath $source)) { throw 'Modern folder dialog implementation missing' }
Add-Type -Path $source
Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class ShellItemFixture {
    [DllImport("shell32.dll", CharSet = CharSet.Unicode, PreserveSig = false)]
    public static extern void SHCreateItemFromParsingName(string path, IntPtr context,
        ref Guid iid, out IntPtr item);
}
'@
$create = [AgentHub.FolderPicker].GetMethod('CreateDialog', [Reflection.BindingFlags]'NonPublic,Static')
$dialog = $null
$item = $null
$pointer = [IntPtr]::Zero
$displayName = [IntPtr]::Zero
$fixturePath = Join-Path $PSScriptRoot ([regex]::Unescape('picker-\u76ee\u5f55 ') + [Guid]::NewGuid().ToString('N'))
try {
    $dialog = $create.Invoke($null, @('AgentHub workspace'))
    $arguments = [object[]]@( [uint32]0 )
    $create.ReturnType.GetMethod('GetOptions').Invoke($dialog, $arguments) | Out-Null
    $options = [uint32]$arguments[0]
    # Folder selection + real file system paths + existing paths + unchanged cwd.
    if (($options -band 0x868) -ne 0x868) { throw 'Dialog allows invalid workspaces' }
    if ($options -band 0x200) { throw 'Dialog allows multiple workspaces' }
    # Check the production IShellItem binding returns a Unicode file system path.
    [IO.Directory]::CreateDirectory($fixturePath) | Out-Null
    $iid = [Guid]'43826D1E-E718-42EE-BC55-A1E261C37BFE'
    [ShellItemFixture]::SHCreateItemFromParsingName($fixturePath, [IntPtr]::Zero, [ref]$iid, [ref]$pointer)
    $item = [Runtime.InteropServices.Marshal]::GetObjectForIUnknown($pointer)
    $itemType = [AgentHub.FolderPicker].GetNestedType('IShellItem', [Reflection.BindingFlags]'NonPublic')
    $arguments = [object[]]@( [Convert]::ToUInt32('80058000',16), [IntPtr]::Zero )
    $itemType.GetMethod('GetDisplayName').Invoke($item, $arguments) | Out-Null
    $displayName = [IntPtr]$arguments[1]
    $path = [Runtime.InteropServices.Marshal]::PtrToStringUni($displayName)
    if ($path -ne $fixturePath) { throw 'Unicode workspace path did not round trip' }
    Write-Output 'modern Windows folder dialog: PASS (no window opened)'
} finally {
    if ($displayName -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::FreeCoTaskMem($displayName) }
    if ($null -ne $item) { [Runtime.InteropServices.Marshal]::FinalReleaseComObject($item) | Out-Null }
    if ($pointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::Release($pointer) | Out-Null }
    if ($null -ne $dialog) { [Runtime.InteropServices.Marshal]::FinalReleaseComObject($dialog) | Out-Null }
    if ([IO.Directory]::Exists($fixturePath)) { [IO.Directory]::Delete($fixturePath) }
}
