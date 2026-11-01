"""Native stacks of another process, by module and exported symbol."""
import ctypes
import os
import sys
from ctypes import wintypes as w

pid = int(sys.argv[1])
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
psapi = ctypes.WinDLL("psapi", use_last_error=True)
dbg = ctypes.WinDLL("dbghelp", use_last_error=True)
k32.OpenProcess.restype = w.HANDLE
k32.OpenThread.restype = w.HANDLE
k32.CreateToolhelp32Snapshot.restype = w.HANDLE
k32.SuspendThread.argtypes = [w.HANDLE]
k32.ResumeThread.argtypes = [w.HANDLE]
k32.GetThreadContext.argtypes = [w.HANDLE, ctypes.c_void_p]
psapi.GetModuleFileNameExW.argtypes = [w.HANDLE, ctypes.c_void_p, w.LPWSTR, w.DWORD]
dbg.SymInitialize.argtypes = [w.HANDLE, ctypes.c_char_p, w.BOOL]
dbg.SymGetModuleBase64.argtypes = [w.HANDLE, ctypes.c_ulonglong]
dbg.SymGetModuleBase64.restype = ctypes.c_ulonglong
dbg.SymFunctionTableAccess64.argtypes = [w.HANDLE, ctypes.c_ulonglong]
dbg.SymFunctionTableAccess64.restype = ctypes.c_void_p


class THREADENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", w.DWORD), ("cntUsage", w.DWORD), ("th32ThreadID", w.DWORD),
        ("th32OwnerProcessID", w.DWORD), ("tpBasePri", w.LONG),
        ("tpDeltaPri", w.LONG), ("dwFlags", w.DWORD),
    ]


class ADDRESS64(ctypes.Structure):
    _fields_ = [
        ("Offset", ctypes.c_ulonglong), ("Segment", ctypes.c_ushort),
        ("Mode", ctypes.c_uint),
    ]


class STACKFRAME64(ctypes.Structure):
    _fields_ = [
        ("AddrPC", ADDRESS64), ("AddrReturn", ADDRESS64), ("AddrFrame", ADDRESS64),
        ("AddrStack", ADDRESS64), ("AddrBStore", ADDRESS64),
        ("FuncTableEntry", ctypes.c_void_p), ("Params", ctypes.c_ulonglong * 4),
        ("Far", w.BOOL), ("Virtual", w.BOOL), ("Reserved", ctypes.c_ulonglong * 3),
        ("KdHelp", ctypes.c_byte * 0x100),
    ]


class SYMBOL_INFO(ctypes.Structure):
    _fields_ = [
        ("SizeOfStruct", w.ULONG), ("TypeIndex", w.ULONG),
        ("Reserved", ctypes.c_ulonglong * 2), ("Index", w.ULONG), ("Size", w.ULONG),
        ("ModBase", ctypes.c_ulonglong), ("Flags", w.ULONG),
        ("Value", ctypes.c_ulonglong), ("Address", ctypes.c_ulonglong),
        ("Register", w.ULONG), ("Scope", w.ULONG), ("Tag", w.ULONG),
        ("NameLen", w.ULONG), ("MaxNameLen", w.ULONG), ("Name", ctypes.c_char * 512),
    ]


process = k32.OpenProcess(0x1F0FFF, False, pid)
if not process:
    sys.exit(f"OpenProcess failed: {ctypes.get_last_error()}")
dbg.SymSetOptions(0x2 | 0x10)  # UNDNAME | DEFERRED_LOADS
dbg.SymInitialize(process, None, True)

snapshot = k32.CreateToolhelp32Snapshot(4, 0)
entry = THREADENTRY32()
entry.dwSize = ctypes.sizeof(entry)
threads = []
more = k32.Thread32First(snapshot, ctypes.byref(entry))
while more:
    if entry.th32OwnerProcessID == pid:
        threads.append(entry.th32ThreadID)
    more = k32.Thread32Next(snapshot, ctypes.byref(entry))
print("threads", threads)


def module_of(base):
    name = ctypes.create_unicode_buffer(520)
    if base and psapi.GetModuleFileNameExW(process, ctypes.c_void_p(base), name, 520):
        return os.path.basename(name.value)
    return "?"


for tid in threads:
    thread = k32.OpenThread(0x1F03FF, False, tid)
    k32.SuspendThread(thread)
    raw = ctypes.create_string_buffer(1232 + 16)
    aligned = (ctypes.addressof(raw) + 15) & ~15
    ctypes.c_uint32.from_address(aligned + 0x30).value = 0x10000B  # CONTEXT_FULL
    if not k32.GetThreadContext(thread, aligned):
        print("GetThreadContext failed", ctypes.get_last_error())
    frame = STACKFRAME64()
    frame.AddrPC.Offset = ctypes.c_uint64.from_address(aligned + 0xF8).value
    frame.AddrFrame.Offset = ctypes.c_uint64.from_address(aligned + 0xA0).value
    frame.AddrStack.Offset = ctypes.c_uint64.from_address(aligned + 0x98).value
    frame.AddrPC.Mode = frame.AddrFrame.Mode = frame.AddrStack.Mode = 3
    print(f"--- thread {tid}")
    for depth in range(48):
        ok = dbg.StackWalk64(
            0x8664, process, thread, ctypes.byref(frame), ctypes.c_void_p(aligned),
            None, ctypes.cast(dbg.SymFunctionTableAccess64, ctypes.c_void_p),
            ctypes.cast(dbg.SymGetModuleBase64, ctypes.c_void_p), None,
        )
        if not ok or not frame.AddrPC.Offset:
            break
        address = frame.AddrPC.Offset
        module = module_of(dbg.SymGetModuleBase64(process, address))
        symbol = SYMBOL_INFO()
        symbol.SizeOfStruct = 88
        symbol.MaxNameLen = 500
        offset = ctypes.c_ulonglong()
        name = "?"
        if dbg.SymFromAddr(
            process, ctypes.c_ulonglong(address), ctypes.byref(offset), ctypes.byref(symbol)
        ):
            name = f"{symbol.Name.decode(errors='replace')}+0x{offset.value:x}"
        print(f"{depth:2d} {module:32s} {name}")
    k32.ResumeThread(thread)
