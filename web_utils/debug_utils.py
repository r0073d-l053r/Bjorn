"""debug_utils.py - Debug/profiling for the Bjorn Debug page.

Exposes process and per-thread metrics via /proc. Optimized for Pi Zero 2.
"""

import json
import os
import sys
import threading
import time
import tracemalloc

from logger import Logger

logger = Logger(name="debug_utils")

_SC_CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100

# ---------------------------------------------------------------------------
# /proc helpers
# ---------------------------------------------------------------------------

def _read_proc_status():
    result = {}
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    result["vm_rss_kb"] = int(line.split()[1])
                elif line.startswith("VmSize:"):
                    result["vm_size_kb"] = int(line.split()[1])
                elif line.startswith("VmPeak:"):
                    result["vm_peak_kb"] = int(line.split()[1])
                elif line.startswith("VmSwap:"):
                    result["vm_swap_kb"] = int(line.split()[1])
                elif line.startswith("FDSize:"):
                    result["fd_slots"] = int(line.split()[1])
                elif line.startswith("Threads:"):
                    result["kernel_threads"] = int(line.split()[1])
                elif line.startswith("RssAnon:"):
                    result["rss_anon_kb"] = int(line.split()[1])
                elif line.startswith("RssFile:"):
                    result["rss_file_kb"] = int(line.split()[1])
                elif line.startswith("RssShmem:"):
                    result["rss_shmem_kb"] = int(line.split()[1])
    except Exception:
        pass
    return result


def _fd_count():
    try:
        return len(os.listdir("/proc/self/fd"))
    except Exception:
        return -1


def _read_open_files():
    """Read open FDs - reuses a single dict to minimize allocations."""
    fd_dir = "/proc/self/fd"
    fd_map = {}
    try:
        fds = os.listdir(fd_dir)
    except Exception:
        return []

    for fd in fds:
        try:
            target = os.readlink(fd_dir + "/" + fd)
        except Exception:
            target = "???"

        if target.startswith("/"):
            ftype = "device" if "/dev/" in target else "proc" if target.startswith("/proc/") else "temp" if (target.startswith("/tmp/") or target.startswith("/run/")) else "file"
        elif target.startswith("socket:"):
            ftype = "socket"
        elif target.startswith("pipe:"):
            ftype = "pipe"
        elif target.startswith("anon_inode:"):
            ftype = "anon"
        else:
            ftype = "other"

        entry = fd_map.get(target)
        if entry is None:
            entry = {"target": target, "type": ftype, "count": 0, "fds": []}
            fd_map[target] = entry
        entry["count"] += 1
        if len(entry["fds"]) < 5:
            entry["fds"].append(int(fd))

    result = sorted(fd_map.values(), key=lambda x: (-x["count"], x["target"]))
    return result


def _read_thread_stats():
    threads = []
    task_dir = "/proc/self/task"
    try:
        tids = os.listdir(task_dir)
    except Exception:
        return threads

    for tid in tids:
        try:
            with open(task_dir + "/" + tid + "/stat", "r", encoding="utf-8") as f:
                raw = f.read()
            i1 = raw.find("(")
            i2 = raw.rfind(")")
            if i1 < 0 or i2 < 0:
                continue
            name = raw[i1 + 1:i2]
            fields = raw[i2 + 2:].split()
            state = fields[0] if fields else "?"
            utime = int(fields[11]) if len(fields) > 11 else 0
            stime = int(fields[12]) if len(fields) > 12 else 0
            threads.append({
                "tid": int(tid),
                "name": name,
                "state": state,
                "cpu_ticks": utime + stime,
            })
        except Exception:
            continue
    return threads


def _get_python_threads_rich():
    """Enumerate Python threads with target + current frame. Minimal allocations."""
    frames = sys._current_frames()
    result = []

    for t in threading.enumerate():
        ident = t.ident
        nid = getattr(t, "native_id", None)

        # Target function info
        target = getattr(t, "_target", None)
        if target is not None:
            tf = getattr(target, "__qualname__", getattr(target, "__name__", "?"))
            tm = getattr(target, "__module__", "")
            # Source file - use __code__ directly (avoids importing inspect)
            tfile = ""
            code = getattr(target, "__code__", None)
            if code:
                tfile = getattr(code, "co_filename", "")
        else:
            tf = "(main)" if t.name == "MainThread" else "(no target)"
            tm = ""
            tfile = ""

        # Current stack - top 5 frames, build compact strings directly
        stack = []
        frame = frames.get(ident)
        depth = 0
        while frame is not None and depth < 5:
            co = frame.f_code
            fn = co.co_filename
            # Shorten: last 2 path components
            sep = fn.rfind("/")
            if sep > 0:
                sep2 = fn.rfind("/", 0, sep)
                short = fn[sep2 + 1:] if sep2 >= 0 else fn
            else:
                short = fn
            stack.append({
                "file": short,
                "line": frame.f_lineno,
                "func": co.co_name,
            })
            frame = frame.f_back
            depth += 1
        # Release frame reference immediately
        del frame

        result.append({
            "name": t.name,
            "daemon": t.daemon,
            "alive": t.is_alive(),
            "ident": ident,
            "native_id": nid,
            "target_func": tf,
            "target_module": tm,
            "target_file": tfile,
            "stack_top": stack,
        })

    # Release all frame references
    del frames
    return result


def _system_cpu_mem():
    result = {"cpu_count": 1, "mem_total_kb": 0, "mem_available_kb": 0}
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    result["mem_total_kb"] = int(line.split()[1])
                elif line.startswith("MemAvailable:"):
                    result["mem_available_kb"] = int(line.split()[1])
    except Exception:
        pass
    try:
        result["cpu_count"] = len(os.sched_getaffinity(0))
    except Exception:
        try:
            result["cpu_count"] = os.cpu_count() or 1
        except Exception:
            pass
    return result


def _read_smaps_rollup():
    """
    Read /proc/self/smaps_rollup for a breakdown of what consumes RSS.
    This shows: Shared_Clean, Shared_Dirty, Private_Clean, Private_Dirty,
    which helps identify C extension memory vs Python heap vs mmap.
    """
    result = {}
    try:
        with open("/proc/self/smaps_rollup", "r", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    key = parts[0].rstrip(":")
                    if key in ("Rss", "Pss", "Shared_Clean", "Shared_Dirty",
                               "Private_Clean", "Private_Dirty", "Referenced",
                               "Anonymous", "Swap", "Locked"):
                        result[key.lower() + "_kb"] = int(parts[1])
    except Exception:
        pass
    return result


# ---------------------------------------------------------------------------
# Cached tracemalloc - take snapshot at most every 5s to reduce overhead
# ---------------------------------------------------------------------------

_tm_cache_lock = threading.Lock()
_tm_cache = None       # (current, peak, by_file, by_line)
_tm_cache_time = 0.0
_TM_CACHE_TTL = 5.0    # seconds


def _get_tracemalloc_cached():
    """Return cached tracemalloc data, refreshing at most every 5s."""
    global _tm_cache, _tm_cache_time

    if not tracemalloc.is_tracing():
        return 0, 0, [], []

    now = time.monotonic()
    with _tm_cache_lock:
        if _tm_cache is not None and (now - _tm_cache_time) < _TM_CACHE_TTL:
            return _tm_cache

    # Take snapshot outside the lock (it's slow)
    current, peak = tracemalloc.get_traced_memory()
    snap = tracemalloc.take_snapshot()

    # Single statistics call - use lineno (more useful), derive file-level client-side
    stats_line = snap.statistics("lineno")[:30]
    top_by_line = []
    file_agg = {}
    for s in stats_line:
        frame = s.traceback[0] if s.traceback else None
        if frame is None:
            continue
        fn = frame.filename
        sep = fn.rfind("/")
        if sep > 0:
            sep2 = fn.rfind("/", 0, sep)
            short = fn[sep2 + 1:] if sep2 >= 0 else fn
        else:
            short = fn
        top_by_line.append({
            "file": short,
            "full_path": fn,
            "line": frame.lineno,
            "size_kb": round(s.size / 1024, 1),
            "count": s.count,
        })
        # Aggregate by file
        if fn not in file_agg:
            file_agg[fn] = {"file": short, "full_path": fn, "size_kb": 0, "count": 0}
        file_agg[fn]["size_kb"] += round(s.size / 1024, 1)
        file_agg[fn]["count"] += s.count

    # Also get file-level stats for files that don't appear in line-level top
    stats_file = snap.statistics("filename")[:20]
    for s in stats_file:
        fn = str(s.traceback) if hasattr(s.traceback, '__str__') else ""
        # traceback for filename stats is just the filename
        raw_fn = s.traceback[0].filename if s.traceback else fn
        if raw_fn not in file_agg:
            sep = raw_fn.rfind("/")
            if sep > 0:
                sep2 = raw_fn.rfind("/", 0, sep)
                short = raw_fn[sep2 + 1:] if sep2 >= 0 else raw_fn
            else:
                short = raw_fn
            file_agg[raw_fn] = {"file": short, "full_path": raw_fn, "size_kb": 0, "count": 0}
        entry = file_agg[raw_fn]
        # Use the larger of aggregated or direct stats
        direct_kb = round(s.size / 1024, 1)
        if direct_kb > entry["size_kb"]:
            entry["size_kb"] = direct_kb
        if s.count > entry["count"]:
            entry["count"] = s.count

    top_by_file = sorted(file_agg.values(), key=lambda x: -x["size_kb"])[:20]

    # Release snapshot immediately
    del snap

    result = (current, peak, top_by_file, top_by_line)
    with _tm_cache_lock:
        _tm_cache = result
        _tm_cache_time = now

    return result


# ---------------------------------------------------------------------------
# Snapshot + history ring buffer
# ---------------------------------------------------------------------------

_MAX_HISTORY = 120
_history_lock = threading.Lock()
_history = []
_prev_thread_ticks = {}
_prev_proc_ticks = 0
_prev_wall = 0.0


def _take_snapshot():
    global _prev_thread_ticks, _prev_proc_ticks, _prev_wall

    now = time.time()
    wall_delta = now - _prev_wall if _prev_wall > 0 else 1.0
    tick_budget = wall_delta * _SC_CLK_TCK

    # Process-level
    status = _read_proc_status()
    fd_open = _fd_count()
    sys_info = _system_cpu_mem()
    smaps = _read_smaps_rollup()

    # Thread CPU from /proc
    raw_threads = _read_thread_stats()
    thread_details = []
    new_ticks_map = {}
    total_proc_ticks = 0

    for t in raw_threads:
        tid = t["tid"]
        prev = _prev_thread_ticks.get(tid, t["cpu_ticks"])
        delta = max(0, t["cpu_ticks"] - prev)
        cpu_pct = (delta / tick_budget * 100.0) if tick_budget > 0 else 0.0
        new_ticks_map[tid] = t["cpu_ticks"]
        total_proc_ticks += t["cpu_ticks"]
        thread_details.append({
            "tid": tid,
            "name": t["name"],
            "state": t["state"],
            "cpu_pct": round(cpu_pct, 2),
            "cpu_ticks_total": t["cpu_ticks"],
        })

    thread_details.sort(key=lambda x: x["cpu_pct"], reverse=True)

    proc_delta = total_proc_ticks - _prev_proc_ticks if _prev_proc_ticks else 0
    proc_cpu_pct = (proc_delta / tick_budget * 100.0) if tick_budget > 0 else 0.0

    _prev_thread_ticks = new_ticks_map
    _prev_proc_ticks = total_proc_ticks
    _prev_wall = now

    # Python threads
    py_threads = _get_python_threads_rich()

    # Match kernel TIDs to Python threads
    native_to_py = {}
    for pt in py_threads:
        nid = pt.get("native_id")
        if nid is not None:
            native_to_py[nid] = pt

    for td in thread_details:
        pt = native_to_py.get(td["tid"])
        if pt:
            td["py_name"] = pt["name"]
            td["py_target"] = pt.get("target_func", "")
            td["py_module"] = pt.get("target_module", "")
            td["py_file"] = pt.get("target_file", "")
            if pt.get("stack_top"):
                top = pt["stack_top"][0]
                td["py_current"] = f"{top['file']}:{top['line']} {top['func']}()"

    # tracemalloc (cached, refreshes every 5s)
    tm_current, tm_peak, tm_by_file, tm_by_line = _get_tracemalloc_cached()

    # Open files
    open_files = _read_open_files()

    # Memory breakdown
    rss_kb = status.get("vm_rss_kb", 0)
    tm_current_kb = round(tm_current / 1024, 1)
    # C/native memory = RSS - Python traced (approximation)
    rss_anon_kb = status.get("rss_anon_kb", 0)
    rss_file_kb = status.get("rss_file_kb", 0)

    snapshot = {
        "ts": round(now, 3),
        "proc_cpu_pct": round(proc_cpu_pct, 2),
        "rss_kb": rss_kb,
        "vm_size_kb": status.get("vm_size_kb", 0),
        "vm_peak_kb": status.get("vm_peak_kb", 0),
        "vm_swap_kb": status.get("vm_swap_kb", 0),
        "fd_open": fd_open,
        "fd_slots": status.get("fd_slots", 0),
        "kernel_threads": status.get("kernel_threads", 0),
        "py_thread_count": len(py_threads),
        "sys_cpu_count": sys_info["cpu_count"],
        "sys_mem_total_kb": sys_info["mem_total_kb"],
        "sys_mem_available_kb": sys_info["mem_available_kb"],
        # Memory breakdown
        "rss_anon_kb": rss_anon_kb,
        "rss_file_kb": rss_file_kb,
        "rss_shmem_kb": status.get("rss_shmem_kb", 0),
        "private_dirty_kb": smaps.get("private_dirty_kb", 0),
        "private_clean_kb": smaps.get("private_clean_kb", 0),
        "shared_dirty_kb": smaps.get("shared_dirty_kb", 0),
        "shared_clean_kb": smaps.get("shared_clean_kb", 0),
        # Data
        "threads": thread_details,
        "py_threads": py_threads,
        "tracemalloc_active": tracemalloc.is_tracing(),
        "tracemalloc_current_kb": tm_current_kb,
        "tracemalloc_peak_kb": round(tm_peak / 1024, 1),
        "tracemalloc_by_file": tm_by_file,
        "tracemalloc_by_line": tm_by_line,
        "open_files": open_files,
    }

    with _history_lock:
        _history.append({
            "ts": snapshot["ts"],
            "proc_cpu_pct": snapshot["proc_cpu_pct"],
            "rss_kb": rss_kb,
            "fd_open": fd_open,
            "py_thread_count": snapshot["py_thread_count"],
            "kernel_threads": snapshot["kernel_threads"],
            "vm_swap_kb": snapshot["vm_swap_kb"],
            "private_dirty_kb": snapshot["private_dirty_kb"],
        })
        if len(_history) > _MAX_HISTORY:
            del _history[: len(_history) - _MAX_HISTORY]

    return snapshot


# ---------------------------------------------------------------------------
# WebUtils class
# ---------------------------------------------------------------------------

class DebugUtils:
    def __init__(self, shared_data):
        self.shared_data = shared_data

    def get_snapshot(self, handler):
        try:
            data = _take_snapshot()
            self._send_json(handler, data)
        except Exception as exc:
            logger.error(f"debug snapshot error: {exc}")
            self._send_json(handler, {"error": str(exc)}, status=500)

    def get_history(self, handler):
        try:
            with _history_lock:
                data = list(_history)
            self._send_json(handler, {"history": data})
        except Exception as exc:
            logger.error(f"debug history error: {exc}")
            self._send_json(handler, {"error": str(exc)}, status=500)

    def toggle_tracemalloc(self, data):
        global _tm_cache, _tm_cache_time
        action = data.get("action", "status")
        try:
            if action == "start":
                if not tracemalloc.is_tracing():
                    tracemalloc.start(int(data.get("nframes", 10)))
                return {"status": "ok", "tracing": True}
            elif action == "stop":
                if tracemalloc.is_tracing():
                    tracemalloc.stop()
                with _tm_cache_lock:
                    _tm_cache = None
                    _tm_cache_time = 0.0
                return {"status": "ok", "tracing": False}
            else:
                return {"status": "ok", "tracing": tracemalloc.is_tracing()}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    def get_gc_stats(self, handler):
        import gc
        try:
            counts = gc.get_count()
            thresholds = gc.get_threshold()
            self._send_json(handler, {
                "gc_enabled": gc.isenabled(),
                "counts": {"gen0": counts[0], "gen1": counts[1], "gen2": counts[2]},
                "thresholds": {"gen0": thresholds[0], "gen1": thresholds[1], "gen2": thresholds[2]},
            })
        except Exception as exc:
            self._send_json(handler, {"error": str(exc)}, status=500)

    def force_gc(self, data):
        import gc
        try:
            return {"status": "ok", "collected": gc.collect()}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    @staticmethod
    def _send_json(handler, data, status=200):
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.end_headers()
        handler.wfile.write(json.dumps(data, default=str).encode("utf-8"))
