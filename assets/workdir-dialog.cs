using System;
using System.Runtime.InteropServices;

namespace AgentHub
{
    // Windows Common Item Dialog in folder mode. No additional runtime required.
    public static class FolderPicker
    {
        private static IFileDialog CreateDialog(string title)
        {
            var type = Type.GetTypeFromCLSID(new Guid("DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7"));
            var dialog = (IFileDialog)Activator.CreateInstance(type);
            try
            {
                uint options;
                dialog.GetOptions(out options);
                // PICKFOLDERS | FORCEFILESYSTEM | PATHMUSTEXIST | NOCHANGEDIR
                dialog.SetOptions(options | 0x20 | 0x40 | 0x800 | 0x8);
                dialog.SetTitle(title);
                return dialog;
            }
            catch
            {
                Marshal.FinalReleaseComObject(dialog);
                throw;
            }
        }

        public static string Pick(IntPtr owner, string title)
        {
            var dialog = CreateDialog(title);
            IShellItem item = null;
            IntPtr path = IntPtr.Zero;
            try
            {
                int result = dialog.Show(owner);
                if (result == unchecked((int)0x800704C7)) return null; // User cancelled.
                Marshal.ThrowExceptionForHR(result);
                dialog.GetResult(out item);
                item.GetDisplayName(0x80058000, out path); // SIGDN_FILESYSPATH
                return Marshal.PtrToStringUni(path);
            }
            finally
            {
                if (path != IntPtr.Zero) Marshal.FreeCoTaskMem(path);
                if (item != null) Marshal.FinalReleaseComObject(item);
                Marshal.FinalReleaseComObject(dialog);
            }
        }

        // Methods must remain in native vtable order, including unused slots.
        [ComImport, Guid("42F85136-DB7E-439C-85F1-E4075D135FC8"),
         InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
        private interface IFileDialog
        {
            [PreserveSig] int Show(IntPtr owner);
            void SetFileTypes(uint count, IntPtr filters);
            void SetFileTypeIndex(uint index);
            void GetFileTypeIndex(out uint index);
            void Advise(IntPtr events, out uint cookie);
            void Unadvise(uint cookie);
            void SetOptions(uint options);
            void GetOptions(out uint options);
            void SetDefaultFolder(IShellItem item);
            void SetFolder(IShellItem item);
            void GetFolder(out IShellItem item);
            void GetCurrentSelection(out IShellItem item);
            void SetFileName([MarshalAs(UnmanagedType.LPWStr)] string name);
            void GetFileName(out IntPtr name);
            void SetTitle([MarshalAs(UnmanagedType.LPWStr)] string title);
            void SetOkButtonLabel([MarshalAs(UnmanagedType.LPWStr)] string text);
            void SetFileNameLabel([MarshalAs(UnmanagedType.LPWStr)] string text);
            void GetResult(out IShellItem item);
            void AddPlace(IShellItem item, uint location);
            void SetDefaultExtension([MarshalAs(UnmanagedType.LPWStr)] string extension);
            void Close(int result);
            void SetClientGuid(ref Guid guid);
            void ClearClientData();
            void SetFilter(IntPtr filter);
        }

        [ComImport, Guid("43826D1E-E718-42EE-BC55-A1E261C37BFE"),
         InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
        private interface IShellItem
        {
            void BindToHandler(IntPtr context, ref Guid handler, ref Guid iid, out IntPtr result);
            void GetParent(out IShellItem parent);
            void GetDisplayName(uint kind, out IntPtr name);
            void GetAttributes(uint mask, out uint attributes);
            void Compare(IShellItem other, uint hint, out int order);
        }
    }
}
