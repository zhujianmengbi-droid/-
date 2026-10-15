# -*- coding: utf-8 -*-
"""TaskbarSwitcher 任务栏顺序枚举与窗口激活逻辑，供主 GUI 内嵌调用。"""
import ctypes as ct
from ctypes import wintypes as wt
import re
import time

try:
    import uiautomation as auto
except ImportError:  # Keep the main GUI usable if an optional plugin is absent.
    auto = None


u = ct.WinDLL("user32", use_last_error=True)
k = ct.WinDLL("kernel32", use_last_error=True)
dwm = ct.WinDLL("dwmapi", use_last_error=True)
CALLBACK = ct.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012


def bind(dll, name, args, result):
    function = getattr(dll, name)
    function.argtypes, function.restype = args, result
    return function


bind(u, "FindWindowW", [wt.LPCWSTR, wt.LPCWSTR], wt.HWND)
bind(u, "EnumChildWindows", [wt.HWND, CALLBACK, wt.LPARAM], wt.BOOL)
bind(u, "GetClassNameW", [wt.HWND, wt.LPWSTR, ct.c_int], ct.c_int)
bind(u, "GetWindowTextW", [wt.HWND, wt.LPWSTR, ct.c_int], ct.c_int)
bind(u, "GetForegroundWindow", [], wt.HWND)
bind(u, "EnumWindows", [CALLBACK, wt.LPARAM], wt.BOOL)
for _name in ("IsWindowVisible", "IsIconic", "SetForegroundWindow"):
    bind(u, _name, [wt.HWND], wt.BOOL)
bind(u, "ShowWindow", [wt.HWND, ct.c_int], wt.BOOL)
bind(u, "GetWindowLongW", [wt.HWND, ct.c_int], wt.LONG)
bind(u, "GetWindow", [wt.HWND, wt.UINT], wt.HWND)
bind(u, "GetAsyncKeyState", [ct.c_int], ct.c_short)
bind(u, "GetCursorPos", [ct.POINTER(wt.POINT)], wt.BOOL)
bind(u, "SetCursorPos", [ct.c_int, ct.c_int], wt.BOOL)
bind(u, "RegisterHotKey", [wt.HWND, ct.c_int, wt.UINT, wt.UINT], wt.BOOL)
bind(u, "UnregisterHotKey", [wt.HWND, ct.c_int], wt.BOOL)
bind(u, "PeekMessageW", [ct.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT, wt.UINT], wt.BOOL)
bind(u, "GetMessageW", [ct.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT], wt.BOOL)
bind(u, "PostThreadMessageW", [wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM], wt.BOOL)
bind(k, "GetCurrentThreadId", [], wt.DWORD)
bind(dwm, "DwmGetWindowAttribute", [wt.HWND, wt.DWORD, ct.c_void_p, wt.DWORD], wt.LONG)


def window_text(hwnd):
    buffer = ct.create_unicode_buffer(4096)
    u.GetWindowTextW(hwnd, buffer, len(buffer))
    return buffer.value


def _title_matches(actual_title, expected_title):
    """判断窗口标题与任务栏按钮标题是否对应同一目标窗口（支持子标题/应用后缀匹配）。"""
    actual = (actual_title or "").strip()
    expected = (expected_title or "").strip()
    if not actual or not expected:
        return False
    if actual == expected:
        return True
    actual_cf = actual.casefold()
    expected_cf = expected.casefold()
    if actual_cf == expected_cf:
        return True
    if actual_cf.endswith(" - " + expected_cf) or expected_cf.endswith(" - " + actual_cf):
        return True
    if expected_cf in actual_cf or actual_cf in expected_cf:
        return True
    return False


def child_handles(hwnd):
    result = []

    @CALLBACK
    def visit(handle, _):
        buffer = ct.create_unicode_buffer(256)
        u.GetClassNameW(handle, buffer, len(buffer))
        result.append((handle, buffer.value))
        return True

    u.EnumChildWindows(hwnd, visit, 0)
    return result


def taskbar_items():
    """按 Windows 主任务栏真实顺序返回正在运行的软件，从右往左排列。"""
    if auto is None:
        raise RuntimeError("快捷工具插件依赖未安装，请重新下载完整安装包。")
    taskbar = u.FindWindowW("Shell_TrayWnd", None)
    if not taskbar:
        raise RuntimeError("未找到主任务栏，请等待资源管理器启动后刷新。")

    handles = child_handles(taskbar)
    modern = []
    for handle, class_name in handles:
        if class_name == "Windows.UI.Composition.DesktopWindowContentBridge":
            modern.extend(
                control for control, _depth in auto.WalkControl(
                    auto.ControlFromHandle(handle), maxDepth=6)
                if control.ClassName == "Taskbar.TaskListButtonAutomationPeer")

    for handle, class_name in handles:
        if class_name != "MSTaskListWClass":
            continue
        controls = [control for control in auto.ControlFromHandle(handle).GetChildren()
                    if control.ControlTypeName in ("ButtonControl", "MenuItemControl")]
        items = []
        for index, control in enumerate(controls):
            legacy = control.GetLegacyIAccessiblePattern()
            # 固定但未运行的软件没有 STATE_SYSTEM_HASPOPUP 标志。
            if not legacy.State & 0x40000000:
                continue
            name = control.Name
            title = re.sub(
                r"\s+-\s+\d+\s+(?:个运行窗口|running windows?).*$", "", name, flags=re.I)
            items.append({
                "name": name,
                "title": title,
                "group": control.ControlTypeName == "MenuItemControl",
                "control": modern[index] if len(modern) == len(controls) else control,
                "modern": len(modern) == len(controls),
            })
        return list(reversed(items))

    raise RuntimeError("当前任务栏未提供兼容的窗口列表接口，无法确定真实顺序。")


def visible_windows():
    result = []

    @CALLBACK
    def visit(hwnd, _):
        if not u.IsWindowVisible(hwnd):
            return True
        style = u.GetWindowLongW(hwnd, -20)
        if style & 0x80 or (u.GetWindow(hwnd, 4) and not style & 0x40000):
            return True
        cloaked = wt.DWORD()
        dwm.DwmGetWindowAttribute(hwnd, 14, ct.byref(cloaked), ct.sizeof(cloaked))
        title = window_text(hwnd)
        if title and not cloaked.value:
            result.append((hwnd, title))
        return True

    u.EnumWindows(visit, 0)
    return result


def activate_window(hwnd):
    if u.GetForegroundWindow() == hwnd and not u.IsIconic(hwnd):
        return True
    if u.IsIconic(hwnd):
        u.ShowWindow(hwnd, 9)
    u.SetForegroundWindow(hwnd)
    for _ in range(20):
        if u.GetForegroundWindow() == hwnd and not u.IsIconic(hwnd):
            return True
        time.sleep(.025)
    return False


def activate_item(item):
    # 等待所有修饰键（Shift / Ctrl / Alt / LWin / RWin）释放，避免 Shift+Click
    # 被任务栏识别为“启动新应用实例”，或被目标窗口当成组合快捷键。
    modifier_keys = (0x10, 0x11, 0x12, 0x5B, 0x5C)
    for _ in range(100):
        if not any(u.GetAsyncKeyState(key) & 0x8000 for key in modifier_keys):
            break
        time.sleep(.01)

    windows = visible_windows()
    for hwnd, title in windows:
        if title == item["title"]:
            if not activate_window(hwnd):
                raise RuntimeError("Windows 未允许激活此窗口；目标可能以管理员权限运行。")
            return hwnd
    if not item["group"]:
        for hwnd, title in windows:
            if _title_matches(title, item["title"]):
                if activate_window(hwnd):
                    return hwnd

    previous_foreground = u.GetForegroundWindow()

    def open_button():
        if item["modern"]:
            point = wt.POINT()
            u.GetCursorPos(ct.byref(point))
            try:
                item["control"].Click(simulateMove=False, waitTime=.1)
            finally:
                u.SetCursorPos(point.x, point.y)
        else:
            item["control"].GetLegacyIAccessiblePattern().DoDefaultAction()

    open_button()
    if item["group"]:
        for attempt in range(24):
            if attempt == 12:
                open_button()
            time.sleep(.05)
            previews = []
            for root in auto.GetRootControl().GetChildren():
                if root.ClassName == "TaskListThumbnailWnd":
                    previews.extend(control for control in root.GetChildren()
                                    if control.ControlTypeName == "ListItemControl")
            if previews:
                for preview in previews:
                    title = preview.Name
                    preview.GetInvokePattern().Invoke()
                    time.sleep(.1)
                    hwnd = u.GetForegroundWindow()
                    if _title_matches(window_text(hwnd), title) or (hwnd and hwnd != previous_foreground):
                        return hwnd
                raise RuntimeError("找到分组预览，但无法激活分组窗口。")
        raise RuntimeError("未能读取分组窗口预览，请刷新后重试。")

    time.sleep(.12)
    hwnd = u.GetForegroundWindow()
    current_title = window_text(hwnd)
    if not _title_matches(current_title, item["title"]) and (not hwnd or hwnd == previous_foreground):
        raise RuntimeError("任务栏已响应，但未确认目标窗口进入前台。")
    return hwnd
