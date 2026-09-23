"""System-tray icon for the group-chat app, on the plain Win32 API (no extra packages).

Tray(icon_path, tip, on_show, on_quit).start() runs its own message loop in a thread.
Left click shows the window; right click offers 显示 / 退出.
A second launch of the app finds the tray window by class name and asks it to show the window (bring_to_front).
"""
import ctypes, threading
from ctypes import wintypes as W

u32, s32, k32 = ctypes.windll.user32, ctypes.windll.shell32, ctypes.windll.kernel32
LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, W.HWND, W.UINT, W.WPARAM, W.LPARAM)
CLASS = "GroupChatTrayWindow"
WM_TRAY, WM_SHOW = 0x8000 + 1, 0x8000 + 2
WM_LBUTTONUP, WM_RBUTTONUP, WM_COMMAND, WM_DESTROY = 0x0202, 0x0205, 0x0111, 0x0002
NIM_ADD, NIM_DELETE, NIF_MESSAGE, NIF_ICON, NIF_TIP = 0, 2, 1, 2, 4
CMD_SHOW, CMD_QUIT = 1, 2

u32.DefWindowProcW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
u32.DefWindowProcW.restype = LRESULT
u32.CreateWindowExW.restype = W.HWND
u32.CreateWindowExW.argtypes = [W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                W.HWND, W.HMENU, W.HINSTANCE, W.LPVOID]
u32.LoadImageW.restype = W.HANDLE
u32.CreatePopupMenu.restype = W.HMENU
u32.FindWindowW.restype = W.HWND
u32.TrackPopupMenu.argtypes = [W.HMENU, W.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, W.HWND, W.LPVOID]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", W.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", W.HINSTANCE), ("hIcon", W.HICON), ("hCursor", W.HANDLE), ("hbrBackground", W.HBRUSH),
                ("lpszMenuName", W.LPCWSTR), ("lpszClassName", W.LPCWSTR)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", W.DWORD), ("hWnd", W.HWND), ("uID", W.UINT), ("uFlags", W.UINT), ("uCallbackMessage", W.UINT),
                ("hIcon", W.HICON), ("szTip", W.WCHAR * 128), ("dwState", W.DWORD), ("dwStateMask", W.DWORD),
                ("szInfo", W.WCHAR * 256), ("uVersion", W.UINT), ("szInfoTitle", W.WCHAR * 64), ("dwInfoFlags", W.DWORD),
                ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", W.HICON)]


def bring_to_front():
    """If the app already runs, ask its tray window to show the main window. Returns True if one was found."""
    h = u32.FindWindowW(CLASS, None)
    if h:
        u32.PostMessageW(h, WM_SHOW, 0, 0)
        return True
    return False


class Tray:
    def __init__(self, icon_path, tip, on_show, on_quit):
        self.icon_path, self.tip, self.on_show, self.on_quit = icon_path, tip, on_show, on_quit
        self.hwnd, self.nid = None, None
        self._proc = WNDPROC(self._wndproc)  # keep a reference: the callback must outlive the window

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _wndproc(self, hwnd, msg, wp, lp):
        try:
            if msg == WM_TRAY and lp == WM_LBUTTONUP or msg == WM_SHOW:
                self.on_show()
                return 0
            if msg == WM_TRAY and lp == WM_RBUTTONUP:
                self._menu()
                return 0
            if msg == WM_COMMAND:
                if wp & 0xFFFF == CMD_SHOW:
                    self.on_show()
                elif wp & 0xFFFF == CMD_QUIT:
                    self.remove()
                    self.on_quit()
                return 0
        except Exception:
            pass
        return u32.DefWindowProcW(hwnd, msg, wp, lp)

    def _menu(self):
        m = u32.CreatePopupMenu()
        u32.AppendMenuW(m, 0, CMD_SHOW, "显示群聊")
        u32.AppendMenuW(m, 0x800, 0, None)  # separator
        u32.AppendMenuW(m, 0, CMD_QUIT, "退出（关闭两个模型）")
        pt = W.POINT()
        u32.GetCursorPos(ctypes.byref(pt))
        u32.SetForegroundWindow(self.hwnd)
        u32.TrackPopupMenu(m, 0, pt.x, pt.y, 0, self.hwnd, None)
        u32.DestroyMenu(m)

    def _run(self):
        hinst = k32.GetModuleHandleW(None)
        wc = WNDCLASSW(lpfnWndProc=self._proc, hInstance=hinst, lpszClassName=CLASS)
        u32.RegisterClassW(ctypes.byref(wc))
        self.hwnd = u32.CreateWindowExW(0, CLASS, "群聊托盘", 0, 0, 0, 0, 0, None, None, hinst, None)
        hicon = u32.LoadImageW(None, self.icon_path, 1, 0, 0, 0x10 | 0x40)  # IMAGE_ICON, LR_LOADFROMFILE | LR_DEFAULTSIZE
        self.nid = NOTIFYICONDATAW(cbSize=ctypes.sizeof(NOTIFYICONDATAW), hWnd=self.hwnd, uID=1,
                                   uFlags=NIF_MESSAGE | NIF_ICON | NIF_TIP, uCallbackMessage=WM_TRAY, hIcon=hicon, szTip=self.tip)
        s32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self.nid))
        msg = W.MSG()
        while u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            u32.TranslateMessage(ctypes.byref(msg))
            u32.DispatchMessageW(ctypes.byref(msg))

    def remove(self):
        if self.nid:
            s32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self.nid))
            self.nid = None
