"""Background mode: a small icon by the clock (the system tray) instead of a window.

Shared by the bot farm apps. Everything here is optional: if the tray library can't be loaded the app simply
runs as a normal window. Nothing in this file opens a network port; the "show yourself" request between two
copies of the app is a small file in the app's own data folder.
"""
import os
import sys
import threading

TRAY_PIP = ["pystray>=0.19", "pillow>=9.0"]


def tray_ready():
    try:
        import pystray  # noqa: F401
        import PIL  # noqa: F401
        return True
    except Exception:
        return False


# ---------------- one copy at a time ----------------
_LOCK = None


def acquire_single(home):
    """True if this is the only copy of the app running (holds a lock file until the app exits)."""
    global _LOCK
    try:
        os.makedirs(home, exist_ok=True)
        f = open(os.path.join(home, "app.lock"), "a+")
        if sys.platform.startswith("win"):
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _LOCK = f
        return True
    except OSError:
        return False
    except Exception:
        return True  # can't tell: don't block the app from opening


def release_single():
    """Lets go of the lock (before the app restarts itself, so the new copy can take it)."""
    global _LOCK
    try:
        if _LOCK is not None:
            _LOCK.close()
    except Exception:
        pass
    _LOCK = None


def ask_running_copy_to_show(home):
    try:
        with open(os.path.join(home, "show-window.flag"), "w") as f:
            f.write("1")
    except OSError:
        pass


def show_requested(home):
    p = os.path.join(home, "show-window.flag")
    if os.path.exists(p):
        try:
            os.remove(p)
        except OSError:
            pass
        return True
    return False


# ---------------- gentle on the computer ----------------
def set_low_priority(low=True):
    """Background training runs at a lower priority, so anything you're doing always comes first."""
    try:
        if sys.platform.startswith("win"):
            import ctypes
            k = ctypes.windll.kernel32
            k.SetPriorityClass(k.GetCurrentProcess(), 0x4000 if low else 0x20)  # BELOW_NORMAL / NORMAL
        elif low:
            os.nice(5)
    except Exception:
        pass


# ---------------- start with Windows ----------------
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _pythonw():
    exe = sys.executable
    if sys.platform.startswith("win"):
        w = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(w):
            return w
    return exe


def autostart_supported():
    return sys.platform.startswith("win")


def autostart_enabled(name):
    if not autostart_supported():
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, name)
        return True
    except OSError:
        return False


def set_autostart(name, on, script):
    """Adds (or removes) the app to the programs Windows starts when you log in, in background mode."""
    if not autostart_supported():
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if on:
                winreg.SetValueEx(k, name, 0, winreg.REG_SZ, f'"{_pythonw()}" "{os.path.abspath(script)}" --tray')
            else:
                try:
                    winreg.DeleteValue(k, name)
                except OSError:
                    pass
        return True
    except OSError:
        return False


# ---------------- the icon ----------------
class Tray:
    """The icon by the clock. Menu clicks arrive on the tray's own thread, so every action is handed to the
    app's window thread through `post` (never touches the window directly)."""

    def __init__(self, name, icon_png_b64, post, status, actions):
        self.name, self.post, self.status, self.actions = name, post, status, actions
        self.icon_png_b64, self.icon, self.thread = icon_png_b64, None, None

    def start(self):
        import base64
        import io
        import pystray
        from PIL import Image
        img = Image.open(io.BytesIO(base64.b64decode(self.icon_png_b64)))
        a = self.actions
        Item, Menu = pystray.MenuItem, pystray.Menu

        def do(key):
            return lambda icon, item: self.post(a[key])

        menu = Menu(
            Item(lambda item: self.status().get("line", self.name), None, enabled=False),
            Menu.SEPARATOR,
            Item(f"Open {self.name}", do("show"), default=True),
            Item(lambda item: self.status().get("toggle", "Pause training"), do("toggle")),
            Item("Go LIVE (real money)...", do("live"), visible=lambda item: not self.status().get("live")),
            Menu.SEPARATOR,
            Item("Start when Windows starts", do("autostart"), checked=lambda item: self.status().get("autostart", False),
                 visible=autostart_supported()),
            Item(f"Quit {self.name}", do("quit")),
        )
        self.icon = pystray.Icon(self.name, img, self.name, menu)
        self.thread = threading.Thread(target=self.icon.run, daemon=True, name="tray")
        self.thread.start()

    def set_title(self, text):
        try:
            if self.icon is not None:
                self.icon.title = text[:120]  # Windows cuts tooltips at 127 characters
        except Exception:
            pass

    def notify(self, text, title=None):
        try:
            if self.icon is not None and getattr(self.icon, "HAS_NOTIFICATION", True):
                self.icon.notify(text, title or self.name)
        except Exception:
            pass

    def refresh_menu(self):
        try:
            if self.icon is not None:
                self.icon.update_menu()
        except Exception:
            pass

    def stop(self):
        try:
            if self.icon is not None:
                self.icon.stop()
        except Exception:
            pass
        self.icon = None
