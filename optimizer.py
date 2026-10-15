# -*- coding: utf-8 -*-
"""电脑优化助手 —— 核心优化逻辑模块。

1. 内存优化：调用 EmptyWorkingSet 释放所有进程的工作集（长时间不关机时，
   应用不再使用却仍驻留内存的部分会被强制交还系统，明显缓解卡顿）。
2. 垃圾清理：清理临时文件、浏览器缓存、Windows 更新缓存等磁盘垃圾。
"""
import ctypes
import fnmatch
import os
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

import psutil

# ---------------------------------------------------------------- Windows API
PROCESS_SET_QUOTA = 0x0100
PROCESS_QUERY_INFORMATION = 0x0400
SHERB_NOCONFIRMATION = 0x00000001
SHERB_NOPROGRESSUI = 0x00000002
SHERB_NOSOUND = 0x00000004


class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_uint32),
        ("PageFaultCount", ctypes.c_uint32),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


class _SHQUERYRBINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint32),
        ("i64Size", ctypes.c_longlong),
        ("i64NumItems", ctypes.c_longlong),
    ]


_kernel32 = ctypes.windll.kernel32
_psapi = ctypes.windll.psapi
_shell32 = ctypes.windll.shell32
# 明确 Windows API 参数类型，避免 64 位句柄/大小在 ctypes 默认转换中被截断。
_kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
_kernel32.OpenProcess.restype = ctypes.c_void_p
_kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
_kernel32.SetProcessWorkingSetSize.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_size_t]
_kernel32.SetProcessWorkingSetSize.restype = ctypes.c_int
_psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32]
_psapi.GetProcessMemoryInfo.restype = ctypes.c_int
_shell32.ShellExecuteW.restype = ctypes.c_void_p

# ---------------------------------------------------------------- 系统信息


def is_admin() -> bool:
    """当前进程是否以管理员身份运行。"""
    try:
        return bool(_shell32.IsUserAnAdmin())
    except Exception:
        return False


def restart_as_admin() -> bool:
    """UAC 提权后重新启动本程序。成功拉起新进程时返回 True，用户取消或失败时返回 False。"""
    args = sys.argv[1:] if getattr(sys, "frozen", False) else sys.argv
    params = " ".join(f'"{a}"' for a in args)
    try:
        rc = _shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
        return int(rc or 0) > 32
    except Exception:
        return False


def get_uptime_seconds() -> int:
    return int(_kernel32.GetTickCount64() // 1000)


def format_uptime(seconds: int) -> str:
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days} 天 {hours} 小时"
    if hours:
        return f"{hours} 小时 {minutes} 分"
    return f"{minutes} 分钟"


def get_mem_info() -> dict:
    vm = psutil.virtual_memory()
    return {
        "percent": vm.percent,
        "used": vm.used,
        "total": vm.total,
        "available": vm.available,
    }


def get_cpu_percent() -> float:
    return psutil.cpu_percent(interval=0.0)


# ---------------------------------------------------------------- 内存优化


def _process_ws(pid: int) -> int:
    """返回进程当前工作集大小（字节），失败返回 -1。"""
    h = _kernel32.OpenProcess(PROCESS_QUERY_INFORMATION, False, pid)
    if not h:
        return -1
    try:
        pmc = _PROCESS_MEMORY_COUNTERS()
        pmc.cb = ctypes.sizeof(_PROCESS_MEMORY_COUNTERS)
        if _psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb):
            return int(pmc.WorkingSetSize)
    finally:
        _kernel32.CloseHandle(h)
    return -1


def free_memory() -> dict:
    """把所有进程的工作集压缩交还系统，返回释放统计。"""
    freed = 0
    affected = 0
    failed = 0
    for proc in psutil.process_iter(["pid"]):
        pid = proc.info["pid"]
        before = _process_ws(pid)
        if before < 0:
            failed += 1
            continue
        h = _kernel32.OpenProcess(PROCESS_SET_QUOTA, False, pid)
        if not h:
            failed += 1
            continue
        try:
            if _kernel32.SetProcessWorkingSetSize(h, ctypes.c_size_t(-1), ctypes.c_size_t(-1)):
                after = _process_ws(pid)
                if 0 <= after < before:
                    freed += before - after
                    affected += 1
        finally:
            _kernel32.CloseHandle(h)
    return {"freed": freed, "affected": affected, "failed": failed}


def flush_dns() -> bool:
    try:
        subprocess.run(
            ["ipconfig", "/flushdns"],
            capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- 垃圾清理


@dataclass
class CleanTarget:
    key: str
    name: str
    description: str
    paths: list
    kind: str = "files"          # files | command | recycle
    requires_admin: bool = False
    default_on: bool = True
    patterns: tuple = ()         # 可选文件名通配符（为空时匹配目录下全部普通文件）
    scanned: int = 0             # 可清理字节数
    cleaned: int = 0             # 实际已清理字节数
    skipped: int = 0             # 因占用等原因跳过的文件数
    done: bool = False

    def clear(self):
        self.scanned = 0
        self.cleaned = 0
        self.skipped = 0
        self.done = False


def _matches_patterns(fp, patterns: tuple) -> bool:
    """判断文件名是否匹配目标通配符列表。"""
    if not patterns:
        return True
    name = fp.name if hasattr(fp, "name") else os.path.basename(os.fspath(fp))
    lower_name = name.lower()
    return any(fnmatch.fnmatch(lower_name, pat.lower()) for pat in patterns)


def _file_size(fp) -> int:
    """读取文件大小；支持 Path、字符串和 os.DirEntry。"""
    try:
        if hasattr(fp, "stat"):
            return int(fp.stat(follow_symlinks=False).st_size)
        return int(os.stat(os.fspath(fp), follow_symlinks=False).st_size)
    except OSError:
        return 0


def _iter_files(root, patterns: tuple = ()):
    """迭代目录中的普通文件，避免跟随符号链接和重复构造 Path。"""
    pending = [os.fspath(root)]
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            if _matches_patterns(entry, patterns):
                                yield entry
                    except OSError:
                        # 文件可能在扫描过程中被其他程序移动或删除。
                        continue
        except OSError:
            continue


def _path_kind(path):
    """返回路径的 lstat 类型，路径不存在或无权限时返回 None。"""
    try:
        mode = os.stat(os.fspath(path), follow_symlinks=False).st_mode
    except OSError:
        return None
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISDIR(mode):
        return "dir"
    return None


def scan_target(t: CleanTarget) -> int:
    """统计该目标可清理的总字节数。"""
    total = 0
    if t.kind == "files":
        for p in t.paths:
            kind = _path_kind(p)
            if kind == "file":
                if _matches_patterns(p, t.patterns):
                    total += _file_size(p)
                continue
            if kind == "dir":
                for entry in _iter_files(p, t.patterns):
                    total += _file_size(entry)
    elif t.kind == "recycle":
        total = _recycle_bin_size()
    t.scanned = total
    t.done = True
    return total


def _delete_one(fp, t: CleanTarget) -> None:
    try:
        if hasattr(fp, "stat"):
            size = int(fp.stat(follow_symlinks=False).st_size)
        else:
            size = int(os.stat(os.fspath(fp), follow_symlinks=False).st_size)
        os.unlink(fp.path if hasattr(fp, "path") else os.fspath(fp))
        t.cleaned += size
    except OSError:
        t.skipped += 1


def clean_target(t: CleanTarget) -> None:
    """执行清理，占用中的文件自动跳过。"""
    t.cleaned = 0
    t.skipped = 0
    if t.kind == "files":
        for p in t.paths:
            kind = _path_kind(p)
            if kind == "file":
                if _matches_patterns(p, t.patterns):
                    _delete_one(p, t)
                continue
            if kind == "dir":
                for entry in _iter_files(p, t.patterns):
                    _delete_one(entry, t)
    elif t.kind == "command":
        try:
            subprocess.run(
                t.paths, capture_output=True, timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            t.cleaned = 0
        except Exception:
            t.skipped += 1
    elif t.kind == "recycle":
        flags = SHERB_NOCONFIRMATION | SHERB_NOPROGRESSUI | SHERB_NOSOUND
        if _shell32.SHEmptyRecycleBinW(None, None, flags) == 0:
            t.cleaned = t.scanned
        else:
            t.skipped += 1
    t.done = True


def _recycle_bin_size() -> int:
    info = _SHQUERYRBINFO()
    info.cbSize = ctypes.sizeof(_SHQUERYRBINFO)
    if _shell32.SHQueryRecycleBinW(None, ctypes.byref(info)) == 0:
        return max(0, int(info.i64Size))
    return 0


# ---------------------------------------------------------------- 目标清单


def _browser_cache_dirs() -> list:
    local = os.environ.get("LOCALAPPDATA", "")
    base = Path(local)
    patterns = [
        base / "Google" / "Chrome" / "User Data" / "*" / "Cache",
        base / "Google" / "Chrome" / "User Data" / "*" / "Code Cache",
        base / "Microsoft" / "Edge" / "User Data" / "*" / "Cache",
        base / "Microsoft" / "Edge" / "User Data" / "*" / "Code Cache",
    ]
    dirs = []
    for pat in patterns:
        # 通配符位于 User Data/*/Cache，先枚举配置目录再拼接缓存目录。
        profile_parent = pat.parents[1]
        cache_name = pat.name
        if profile_parent.exists():
            dirs += [str(d / cache_name) for d in profile_parent.glob("*")
                     if (d / cache_name).is_dir()]
    if not dirs:  # 兜底：经典默认配置路径
        for d in (
            base / "Google" / "Chrome" / "User Data" / "Default" / "Cache",
            base / "Microsoft" / "Edge" / "User Data" / "Default" / "Cache",
        ):
            if d.is_dir():
                dirs.append(str(d))
    return dirs


def build_targets() -> list:
    user_temp = os.environ.get("TEMP") or str(Path.home() / "AppData" / "Local" / "Temp")
    windir = os.environ.get("WINDIR", r"C:\Windows")
    local = os.environ.get("LOCALAPPDATA", "")
    return [
        CleanTarget(
            key="user_temp", name="用户临时文件",
            description="当前用户 Temp 目录", paths=[user_temp],
        ),
        CleanTarget(
            key="sys_temp", name="系统临时文件",
            description="Windows\\Temp（需管理员）",
            paths=[str(Path(windir) / "Temp")], requires_admin=True,
        ),
        CleanTarget(
            key="browser", name="浏览器缓存",
            description="Chrome / Edge 网页缓存", paths=_browser_cache_dirs(),
        ),
        CleanTarget(
            key="winupdate", name="Windows 更新缓存",
            description="已下载的更新安装包（需管理员）",
            paths=[str(Path(windir) / "SoftwareDistribution" / "Download")],
            requires_admin=True,
        ),
        CleanTarget(
            key="thumb", name="缩略图缓存",
            description="资源管理器缩略图缓存（保留图标缓存）",
            paths=[str(Path(local) / "Microsoft" / "Windows" / "Explorer")],
            patterns=("thumbcache_*.db",),
        ),
        CleanTarget(
            key="dns", name="DNS 缓存",
            description="刷新 DNS 解析缓存（需管理员）",
            paths=["ipconfig", "/flushdns"], kind="command",
            requires_admin=True,
        ),
        CleanTarget(
            key="recycle", name="回收站",
            description="清空回收站（不可恢复）",
            paths=[], kind="recycle", default_on=False,
        ),
    ]


def scan_all(targets: list, progress=None) -> dict:
    """并行扫描所有目标，返回总计字节数。progress(t) 在每项完成后回调。"""
    # 两个扫描线程足够覆盖多个目录，同时给桌面合成和用户前台程序留出 I/O/CPU。
    with ThreadPoolExecutor(max_workers=min(2, max(1, len(targets))),
                            thread_name_prefix="optimizer-scan") as ex:
        futures = {ex.submit(scan_target, t): t for t in targets}
        for fut in as_completed(futures):
            t = futures[fut]
            try:
                fut.result()
            except Exception:
                t.done = True
            if progress:
                progress(t)
    return {"total": sum(t.scanned for t in targets)}


def clean_selected(targets: list, progress=None) -> dict:
    """逐个清理选中的目标。progress(t) 在每项完成后回调。"""
    for t in targets:
        if not t.done:  # 未扫描过的先补一次扫描（用于回收站统计）
            try:
                scan_target(t)
            except Exception:
                pass
        try:
            clean_target(t)
        except Exception:
            t.skipped += 1
            t.done = True
        if progress:
            progress(t)
    return {
        "cleaned": sum(t.cleaned for t in targets),
        "skipped": sum(t.skipped for t in targets),
    }


def format_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} TB"
