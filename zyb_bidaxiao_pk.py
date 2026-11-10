# -*- coding: utf-8 -*-
"""作业帮比大小：默认连续运行并在每局结束后点击继续PK；空格暂停，Ctrl+C停止。"""
from __future__ import annotations

import argparse
import logging
from logging.handlers import RotatingFileHandler
import ctypes
from ctypes import wintypes
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

ADB = r"C:\leidian\LDPlayer14\adb.exe"
LDCONSOLE = r"C:\leidian\LDPlayer14\ldconsole.exe"
PACKAGE = "com.baidu.homework"
TMP_XML = Path(__file__).with_name("_zyb_ui.xml")

CX, CY = 540, 1360
CONTINUE_XY = (732, 1573)
BIDAXIAO_PK_XY = (188, 980)
KOUSUAN_PK_XY = (752, 490)

SWIPE_MS = 60
SERIAL = None
LOGGER = logging.getLogger("zyb")

_dump_fail = 0
_last_adb_restart = 0.0
_FAIL_LIMIT = 3
_log_lines: list[str] = []
_adb_shell: subprocess.Popen | None = None

_paused = False
_space_was_down = False

VK_UP = 0x26
VK_DOWN = 0x28
VK_SPACE = 0x20

user32 = ctypes.windll.user32
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def log_event(msg: str) -> None:
    LOGGER.info(msg)



def current_drive_root() -> Path:
    if getattr(sys, "frozen", False):
        p = Path(sys.executable).resolve()
    else:
        p = Path.cwd().resolve()
    return Path(p.anchor)


def abort_with_log(reason: str) -> None:
    root = current_drive_root()
    path = root / f"zyb_bidaxiao_fail_{time.strftime('%Y%m%d_%H%M%S')}.log"
    devices = ""
    try:
        r = adb(["devices", "-l"], timeout=5)
        devices = (r.stdout.decode("utf-8", "ignore") if r else "") or "(无)"
    except Exception as e:
        devices = f"(devices 失败: {e})"
    shell = "ok" if _shell_ok() else "fail"
    body = "\n".join(
        [
            "作业帮比大小 PK 异常退出",
            f"时间: {_now()}",
            f"原因: {reason}",
            f"连续失败上限: {_FAIL_LIMIT}",
            f"adb shell: {shell}",
            "adb devices:",
            devices.strip(),
            "",
            "最近日志:",
            *(_log_lines[-80:] if _log_lines else ["(无)"]),    
            "",
        ]
    )
    try:
        path.write_text(body, encoding="utf-8")
        print(f"连续失败超过{_FAIL_LIMIT}次，已退出。日志: {path}", flush=True)
    except Exception as e:
        print(f"写日志失败: {e}\n{body}", flush=True)
    sys.exit(1)


def _adb_raw(args: list[str], timeout: float = 3) -> None:
    try:
        subprocess.run([ADB, *args], capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        pass


def adb(args: list[str], timeout: float = 10) -> subprocess.CompletedProcess | None:
    try:
        cmd = [ADB]
        if SERIAL and args and args[0] not in ("devices", "connect", "kill-server", "start-server"):
            cmd += ["-s", SERIAL]
        r = subprocess.run([*cmd, *args], capture_output=True, timeout=timeout)
        if r.returncode:
            log_event(f"ADB失败: {args[:3]}: {r.stderr.decode('utf-8', 'replace').strip()}")
            return None
        return r
    except (subprocess.TimeoutExpired, OSError) as exc:
        log_event(f"ADB异常 {args[:4]}: {exc}")
        return None


def _shell_ok() -> bool:
    r = adb(["shell", "echo", "ok"], timeout=4)
    return r is not None and b"ok" in (r.stdout or b"")


def restart_adb_soft() -> None:
    print("软重启 adb…", flush=True)
    _adb_raw(["kill-server"], 6)
    time.sleep(0.5)
    _adb_raw(["start-server"], 10)
    time.sleep(0.6)
    _adb_raw(["connect", "127.0.0.1:5555"], 5)


def ensure_ldplayer(timeout: float = 90.0) -> bool:
    if _shell_ok():
        return True
    print("未检测到模拟器，尝试启动雷电…", flush=True)
    try:
        subprocess.run(
            [LDCONSOLE, "launch", "--index", "0"],
            capture_output=True,
            timeout=20,
        )
    except Exception as e:
        print("启动雷电失败:", e, flush=True)

    end = time.time() + timeout
    global _last_adb_restart
    while time.time() < end:
        if _shell_ok():
            print("模拟器已连接", flush=True)
            time.sleep(1.5)
            return True
        try:
            subprocess.run(
                [LDCONSOLE, "launch", "--index", "0"],
                capture_output=True,
                timeout=15,
            )
        except Exception:
            pass
        if time.time() - _last_adb_restart > 20:
            restart_adb_soft()
            _last_adb_restart = time.time()
        print("等待模拟器上线…", flush=True)
        time.sleep(3.0)
    print("模拟器连接超时，请先手动打开雷电并进作业帮", flush=True)
    return False


def clear_uiautomator() -> None:
    _adb_raw(["shell", "pkill", "-9", "-f", "uiautomator"], 2)
    _adb_raw(["shell", "killall", "-9", "uiautomator"], 2)


def tap(x: int, y: int) -> None:
    if adb(["shell", "input", "tap", str(x), str(y)], timeout=3) is None:
        raise RuntimeError("点击失败")


def dump_root(retry: int = 1, clear: bool = False) -> ET.Element | None:
    # 一次ADB命令返回XML，避免 dump + pull 的额外往返及陈旧文件。
    for _ in range(retry):
        start = time.perf_counter()
        r = adb(["exec-out", "uiautomator", "dump", "--compressed", "/dev/tty"], timeout=12)
        if r is None:
            adb(["shell", "pkill", "-f", "com.android.commands.uiautomator"], timeout=2)
        root = _parse_dump_bytes(r.stdout if r is not None else None)
        if r is not None and root is None:
            log_event("XML无效: " + r.stdout[-200:].decode("utf-8", "replace"))
            adb(["shell", "pkill", "-f", "com.android.commands.uiautomator"], timeout=2)
        if root is not None:
            TMP_XML.write_bytes(ET.tostring(root, encoding="utf-8", xml_declaration=True))
            log_event(f"界面读取 {time.perf_counter()-start:.3f}s")
            return root
    return None


def _dump_via_ldconsole() -> ET.Element | None:
    try:
        TMP_XML.unlink(missing_ok=True)
        subprocess.run(
            [
                LDCONSOLE,
                "adb",
                "--index",
                "0",
                "--command",
                "shell uiautomator dump /sdcard/ui.xml",
            ],
            capture_output=True,
            timeout=12,
        )
        r = adb(["pull", "/sdcard/ui.xml", str(TMP_XML)], timeout=5)
        if r is not None and TMP_XML.exists() and TMP_XML.stat().st_size > 80:
            return ET.parse(TMP_XML).getroot()
    except Exception:
        pass
    return None


def _parse_dump_bytes(data: bytes | None) -> ET.Element | None:
    if not data:
        return None
    text = data.decode("utf-8", "ignore")
    idx = text.find("<hierarchy")
    if idx < 0:
        idx = text.find("<?xml")
    if idx < 0:
        return None
    try:
        end = text.find("</hierarchy>", idx)
        return ET.fromstring(text[idx:end + len("</hierarchy>")] if end >= 0 else text[idx:])
    except ET.ParseError:
        return None


def all_texts(root: ET.Element) -> list[tuple[str, str]]:
    return [
        (n.attrib.get("text") or "", n.attrib.get("bounds") or "")
        for n in root.iter("node")
    ]


def parse_bounds(b: str):
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", b)
    if not m:
        return None
    return tuple(map(int, m.groups()))


def bounds_center(b: str):
    box = parse_bounds(b)
    if not box:
        return None
    x1, y1, x2, y2 = box
    return (x1 + x2) // 2, (y1 + y2) // 2


def progress_done(root: ET.Element) -> int | None:
    for t, _ in all_texts(root):
        m = re.fullmatch(r"(\d+)/10题", t)
        if m:
            return int(m.group(1))
    return None


def current_question(root: ET.Element) -> tuple[int, int] | None:
    best, best_area = None, -1
    pat = re.compile(r"(\d+)\s*[?？]\s*(\d+)")
    for n in root.iter("node"):
        t = (n.attrib.get("text") or "") or (n.attrib.get("content-desc") or "")
        m = pat.fullmatch(t.strip()) if t else None
        if not m:
            continue
        box = parse_bounds(n.attrib.get("bounds") or "")
        if not box:
            continue
        area = (box[2] - box[0]) * (box[3] - box[1])
        if area > best_area:
            best_area = area
            best = (int(m.group(1)), int(m.group(2)))
    return best


def read_questions_raw(root: ET.Element) -> list[tuple[int, int]]:
    seen, qs = set(), []
    pat = re.compile(r"(\d+)\s*[?？]\s*(\d+)")
    for n in root.iter("node"):
        for key in ("text", "content-desc"):
            t = n.attrib.get(key) or ""
            if not t:
                continue
            m = pat.fullmatch(t.strip())
            if not m:
                m = pat.search(t)
            if not m:
                continue
            p = (int(m.group(1)), int(m.group(2)))
            if p in seen:
                continue
            seen.add(p)
            qs.append(p)
    return qs


def sync_question_order(root: ET.Element) -> list[tuple[int, int]]:
    qs = read_questions_raw(root)
    if not qs:
        return []
    cur = current_question(root)
    if cur is None:
        return qs[:10]
    if cur not in qs:
        return ([cur] + [p for p in qs if p != cur])[:10]
    if qs[-1] == cur and qs[0] != cur:
        qs = list(reversed(qs))
        print("同步: 题序反转")
    elif qs[0] != cur:
        i = qs.index(cur)
        qs = qs[i:] + qs[:i]
        print(f"同步: 旋转到当前题 {cur}")
    if qs[0] != cur:
        qs = [cur] + [p for p in qs if p != cur]
    print(f"同步: 当前={cur} 进度={progress_done(root)}")
    return qs[:10]


def has_text(root: ET.Element, name: str) -> bool:
    return any(t == name for t, _ in all_texts(root))


def judge(a: int, b: int) -> str:
    return ">" if a > b else "<" if a < b else "="


def stroke_coords(op: str) -> tuple[int, int, int, int]:
    s = 170
    if op == ">":
        return CX - s, CY - s, CX + s, CY + s
    if op == "<":
        return CX + s, CY - s, CX - s, CY + s
    return CX - s, CY, CX + s, CY


def get_adb_shell() -> subprocess.Popen:
    global _adb_shell
    if _adb_shell is None or _adb_shell.poll() is not None:
        _adb_shell = subprocess.Popen(
            [ADB, *(["-s", SERIAL] if SERIAL else []), "shell"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
    return _adb_shell


def write_op(op: str) -> None:
    x1, y1, x2, y2 = stroke_coords(op)
    line = f"input swipe {x1} {y1} {x2} {y2} {SWIPE_MS}\n"
    try:
        sh = get_adb_shell()
        assert sh.stdin is not None
        sh.stdin.write(line.encode("utf-8"))
        sh.stdin.flush()
    except Exception:
        adb(
            ["shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(SWIPE_MS)],
            timeout=2.0,
        )


def opposite_op(op: str) -> str:
    return "<" if op == ">" else ">"


def _key_down(vk: int) -> bool:
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def poll_pause_toggle() -> None:
    global _paused, _space_was_down
    down = _key_down(VK_SPACE)
    if down and not _space_was_down:
        _paused = not _paused
        print("【已暂停】按空格继续" if _paused else "【继续运行】", flush=True)
    _space_was_down = down


def wait_if_paused() -> None:
    while _paused:
        poll_pause_toggle()
        time.sleep(0.03)


def wait_arrow_command() -> str:
    """上箭头 → 大于；下箭头 → 小于。先松再按（边沿），无连点。"""
    while _key_down(VK_UP) or _key_down(VK_DOWN):
        poll_pause_toggle()
        wait_if_paused()
        time.sleep(0.003)
    while True:
        poll_pause_toggle()
        wait_if_paused()
        if _key_down(VK_UP):
            return ">"
        if _key_down(VK_DOWN):
            return "<"
        time.sleep(0.001)


def answer_voice_keys(qs: list[tuple[int, int]]) -> None:
    """
    无语音、无连点。等 ↑/↓（先松再按）后：
    先核对指令与答案，不同则取反，再写一笔。
    """
    global _paused, _space_was_down
    _paused = False
    _space_was_down = _key_down(VK_SPACE)

    print(
        "人机模式：↑大于 ↓小于 | 单击（先松再按）| 答题前取反 | 空格暂停",
        flush=True,
    )
    get_adb_shell()

    for i, (a, b) in enumerate(qs):
        wait_if_paused()
        poll_pause_toggle()

        expected = judge(a, b)
        if expected == "=":
            expected = ">"
        hint = "大" if expected == ">" else "小"
        print(f"\n[{i+1}/{len(qs)}] {a} ? {b}  答案「{hint}」  请按 ↑/↓", flush=True)

        cmd = wait_arrow_command()
        if cmd != expected:
            fix = opposite_op(cmd)
            print(f"  指令{cmd}≠答案{expected} → 答题前取反为{fix}", flush=True)
            cmd = fix

        t0 = time.perf_counter()
        write_op(cmd)
        dt = time.perf_counter() - t0
        print(f"  写出{cmd} {dt:.3f}s", flush=True)

        while _key_down(VK_UP) or _key_down(VK_DOWN):
            time.sleep(0.003)


def ensure_app() -> None:
    if not ensure_ldplayer():
        return
    r = adb(["shell", "dumpsys", "window"], timeout=10)
    out = r.stdout.decode("utf-8", "ignore") if r else ""
    if PACKAGE not in out:
        print("拉起作业帮…", flush=True)
        activity = adb(["shell", "cmd", "package", "resolve-activity", "--brief", PACKAGE])
        component = next((line.strip() for line in activity.stdout.decode().splitlines() if "/" in line), None) if activity else None
        if component is None or adb(["shell", "am", "start", "-n", component]) is None:
            raise RuntimeError("无法启动作业帮，请手动打开")
        time.sleep(2.5)


def enter_or_continue() -> None:
    root = dump_root(retry=2)
    if root is None:
        print("进房前 dump 失败，盲点继续PK", flush=True)
        tap(*CONTINUE_XY)
        time.sleep(1.0)
        return
    if read_questions_raw(root):
        print("已在作答页，直接读题", flush=True)
        return
    if has_text(root, "继续PK"):
        print("结算页 → 继续PK", flush=True)
        tap(*CONTINUE_XY)
        time.sleep(1.2)
        return
    if any(t == "口算PK" for t, _ in all_texts(root)):
        print("首页 → 口算PK", flush=True)
        tap(*KOUSUAN_PK_XY)
        time.sleep(1.6)
        tap(*BIDAXIAO_PK_XY)
        time.sleep(1.2)
        tap(*CONTINUE_XY)
        time.sleep(1.0)
        return
    if has_text(root, "比大小"):
        print("竞技场 → 比大小PK", flush=True)
        tap(*BIDAXIAO_PK_XY)
        time.sleep(1.2)
        tap(*CONTINUE_XY)
        time.sleep(1.0)
        return
    tap(*CONTINUE_XY)
    time.sleep(0.8)


def load_round_questions(
    prev_fp: tuple | None, timeout: float = 25.0
) -> list[tuple[int, int]]:
    end = time.time() + timeout
    clicked_continue = False
    dump_streak = 0
    while time.time() < end:
        poll_pause_toggle()
        wait_if_paused()

        if not _shell_ok():
            log_event("shell 不通，等待重连…")
            dump_streak += 1
            if dump_streak >= _FAIL_LIMIT:
                abort_with_log("adb shell 连续不通超过3次")
            ensure_ldplayer(timeout=20)
            continue

        root = dump_root(retry=2, clear=False)
        if root is None:
            dump_streak += 1
            log_event(f"dump失败 ({dump_streak}/{_FAIL_LIMIT})")
            if dump_streak >= _FAIL_LIMIT:
                abort_with_log("uiautomator dump 连续失败超过3次")
            time.sleep(0.8)
            if dump_streak >= 2:
                clear_uiautomator()
            continue

        dump_streak = 0
        texts = [t for t, _ in all_texts(root)]
        if "继续PK" in texts:
            tap(*CONTINUE_XY)
            clicked_continue = True
            prev_fp = None
            log_event("点击 继续PK，等待出题…")
            time.sleep(1.0)
            continue

        qs = sync_question_order(root)
        done = progress_done(root)
        if not qs:
            if any("手写" in t or "匹配" in t or t == "vs" for t in texts):
                log_event(f"等待题目中… 进度={done}")
            time.sleep(0.45)
            continue

        fp = tuple(qs)
        if prev_fp is not None and fp == prev_fp:
            time.sleep(0.4)
            continue

        if len(qs) >= 10:
            log_event(f"读到 {len(qs)} 题，进度={done}")
            return qs[:10]
        if len(qs) >= 1 and (done in (None, 0) or clicked_continue):
            log_event(f"读到 {len(qs)} 题(不完整)，进度={done}，先用这些")
            return qs
        if len(qs) >= 1 and done is not None and done < 10:
            log_event(f"中途读到剩余 {len(qs)} 题，进度={done}")
            return qs

        time.sleep(0.35)
    return []


def wait_result_and_continue() -> None:
    time.sleep(1.0)
    clear_uiautomator()
    tap(*CONTINUE_XY)
    print("点击 继续PK(无dump)")
    time.sleep(0.4)


def click_text(root: ET.Element, name: str) -> bool:
    for text, bounds in all_texts(root):
        xy = bounds_center(bounds)
        if text == name and xy:
            prepare_input()
            time.sleep(0.15)
            tap(*xy)
            log_event(f"点击: {name}")
            return True
    return False


def answer_checked(op: str) -> None:
    # 保留原脚本的笔画坐标，使用同步命令以检查是否发送成功。
    coords = stroke_coords(op)
    if adb(["shell", "input", "swipe", *map(str, coords), str(SWIPE_MS)], timeout=5) is None:
        raise RuntimeError("划线发送失败，停止以避免重复提交")


def batch_questions(root: ET.Element) -> list[tuple[int, int]]:
    # 不按数字去重：一局可能重复出现同一道题；同节点text/desc只读一次。
    qs = []
    for node in root.iter("node"):
        text = node.get("text") or node.get("content-desc") or ""
        match = re.fullmatch(r"\s*(\d+)\s*[?？]\s*(\d+)\s*", text)
        if match:
            qs.append(tuple(map(int, match.groups())))
    cur = current_question(root)
    if len(qs) == 10 and cur is not None:
        if qs[0] != cur and qs[-1] == cur:
            qs.reverse()
        elif qs[0] != cur:
            return []  # 无法确认题序，禁止猜测。
    return qs


def prepare_input() -> None:
    subprocess.run([ADB, *(["-s", SERIAL] if SERIAL else []), "shell", "pkill", "-f", "com.android.commands.uiautomator"], capture_output=True, timeout=3)


def _find_render_window() -> int:
    """找到雷电的画面子窗口；PrintWindow 即使被遮挡也能在约30ms内取帧。"""
    user32 = ctypes.windll.user32
    enum_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    parents: list[int] = []

    def visit_top(hwnd, _):
        title = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, title, 256)
        if "雷电" in title.value or "LDPlayer" in title.value:
            parents.append(hwnd)
        return True

    user32.EnumWindows(enum_type(visit_top), 0)
    for parent in parents:
        renders: list[int] = []

        def visit_child(hwnd, _):
            name = ctypes.create_unicode_buffer(128)
            user32.GetClassNameW(hwnd, name, 128)
            if name.value == "RenderWindow":
                renders.append(hwnd)
            return True

        user32.EnumChildWindows(parent, enum_type(visit_child), 0)
        if renders:
            return renders[0]
    raise RuntimeError("找不到雷电画面窗口，无法低延迟判断开局")


def wait_start(root: ET.Element, timeout: float, grace: float) -> float:
    """连续截取计时小区域；单帧确认，避免旧版等待两次变化造成数秒延迟。"""
    from io import BytesIO
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("低延迟开局检测需要 Pillow") from exc
    timer = next((n for n in root.iter("node") if re.fullmatch(r"\d{2}:\d{2}", n.get("text", ""))), None)
    texts = [t for t, _ in all_texts(root)]
    if timer is None:
        raise RuntimeError("找不到开局计时区，停止以避免过早提交")
    if timer.get("text") != "00:00" and "题目数量" not in texts:
        return time.perf_counter()
    box = parse_bounds(timer.get("bounds", ""))
    if not box:
        raise RuntimeError("计时区坐标无效")
    deadline = time.monotonic() + timeout
    log_event("答案和输入通道均已准备，单帧监测开局计时区")
    while time.monotonic() < deadline:
        poll_pause_toggle()
        wait_if_paused()
        capture_started = time.perf_counter()
        result = adb(["exec-out", "screencap", "-p"], timeout=3)
        if result is None:
            continue
        image = Image.open(BytesIO(result.stdout)).convert("RGB").crop(box)
        visible = sum(1 for red, green, blue in image.getdata()
                      if green > 130 and blue > red + 25 and green > red + 15)
        if visible >= 20:
            if grace:
                time.sleep(grace)
            log_event(f"检测到开局，反应保护={grace:.3f}s，开始提交")
            # 截图在本次调用期间生成；从调用开始计时，避免低估反应延迟。
            return capture_started
    raise TimeoutError("未检测到计时区出现，不发送答案")


def prepare_batch_sender(qs, gap: float):
    prepare_input()
    # uiautomator 退出后短暂保留输入独占；放在倒计时阶段，不计入开局延迟。
    time.sleep(0.2)
    commands = []
    for i, (a, b) in enumerate(qs):
        op = judge(a, b)
        start_x = CX - 170 if op == ">" else CX + 170
        vertex_x = CX + 170 if op == ">" else CX - 170
        commands.extend([
            f"cmd input motionevent DOWN {start_x} {CY-170} || exit 1",
            "sleep 0.006",
            f"cmd input motionevent MOVE {vertex_x} {CY} || exit 1",
            "sleep 0.006",
            f"cmd input motionevent MOVE {start_x} {CY+170} || exit 1",
            "sleep 0.006",
            f"cmd input motionevent UP {start_x} {CY+170} || exit 1",
            f"echo SENT_{i+1}",
        ])
        if gap and i < len(qs) - 1:
            commands.append(f"sleep {gap:.3f}")
    process = subprocess.Popen(
        [ADB, *(["-s", SERIAL] if SERIAL else []), "shell"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
    )
    assert process.stdin is not None and process.stdout is not None
    process.stdin.write(b"echo READY\n")
    process.stdin.flush()
    if process.stdout.readline().strip() != b"READY":
        process.terminate()
        raise RuntimeError("无法预先建立ADB输入通道")
    return process, "\n".join(commands)


def send_batch(qs, gap: float, prepared_sender=None, signal_time: float | None = None) -> None:
    if any(judge(a, b) == "=" for a, b in qs):
        raise ValueError("相等题目不支持原划线方式")
    sender, script = prepared_sender or prepare_batch_sender(qs, gap)
    assert sender.stdin is not None and sender.stdout is not None
    start = time.perf_counter()
    sender.stdin.write((script + "\nexit\n").encode())
    sender.stdin.close()
    sender.wait(timeout=max(10, len(qs) * (gap + 0.8)))
    output = sender.stdout.read()
    elapsed = time.perf_counter() - start
    total = time.perf_counter() - signal_time if signal_time is not None else elapsed
    count = output.count(b"SENT_")
    log_event(f"批量提交 {count}/{len(qs)} 题，输入耗时={elapsed:.3f}s，开局信号至完成={total:.3f}s，间隔={gap}s")
    if count != len(qs):
        raise RuntimeError("批量提交未全部完成，停止且不自动重发")



CAPTCHA_EXPR_RE = re.compile(r"(\d+)\s*([+\-−])\s*(\d+)\s*=?")


@dataclass
class CaptchaInfo:
    expr: str
    answer: int
    input_xy: tuple[int, int] | None
    confirm_xy: tuple[int, int] | None


def eval_captcha_expr(expr: str) -> int | None:
    m = CAPTCHA_EXPR_RE.fullmatch(expr.strip()) or CAPTCHA_EXPR_RE.search(expr)
    if not m:
        return None
    a, op, b = int(m.group(1)), m.group(2), int(m.group(3))
    if op == "+":
        return a + b
    return a - b


def detect_captcha(root: ET.Element) -> CaptchaInfo | None:
    texts = all_texts(root)
    titled = any(t == "人机验证" for t, _ in texts)
    expr = None
    expr_bounds = None
    for t, b in texts:
        if not t:
            continue
        m = CAPTCHA_EXPR_RE.fullmatch(t.strip())
        if m:
            expr = t.strip()
            expr_bounds = b
            break
    if not titled and expr is None:
        return None
    if expr is None:
        return None
    answer = eval_captcha_expr(expr)
    if answer is None:
        return None
    input_xy = None
    confirm_xy = None
    for n in root.iter("node"):
        text = (n.attrib.get("text") or "").strip()
        cls = n.attrib.get("class") or ""
        xy = bounds_center(n.attrib.get("bounds") or "")
        if not xy:
            continue
        if "EditText" in cls or text == "请输入正确答案":
            input_xy = xy
        if text == "确认":
            confirm_xy = xy
    if input_xy is None and expr_bounds:
        box = parse_bounds(expr_bounds)
        if box:
            input_xy = ((box[0] + box[2]) // 2, min(box[3] + 80, 1800))
    if confirm_xy is None and input_xy is not None:
        confirm_xy = (input_xy[0], min(input_xy[1] + 100, 1850))
    return CaptchaInfo(expr=expr, answer=answer, input_xy=input_xy, confirm_xy=confirm_xy)


def save_captcha_screenshot(log_dir: Path) -> Path | None:
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"captcha_{time.strftime('%Y%m%d_%H%M%S')}.png"
    r = adb(["exec-out", "screencap", "-p"], timeout=8)
    if r is None or not r.stdout:
        log_event("人机验证截图失败")
        return None
    path.write_bytes(r.stdout)
    log_event(f"人机验证截图: {path}")
    return path


def input_captcha_answer(answer: int) -> None:
    text = str(answer)
    if adb(["shell", "input", "text", text], timeout=5) is not None:
        return
    log_event("input text 失败，改用 keyevent 逐位输入")
    if text.startswith("-"):
        adb(["shell", "input", "keyevent", "69"], timeout=3)
        text = text[1:]
    for ch in text:
        code = 7 + int(ch)
        if adb(["shell", "input", "keyevent", str(code)], timeout=3) is None:
            raise RuntimeError(f"输入答案失败: {answer}")


def solve_captcha_math(log_dir: Path, max_attempts: int = 3) -> bool:
    """慢节奏完成加减法验证；成功离开验证页返回 True。"""
    for attempt in range(1, max_attempts + 1):
        root = dump_root(retry=2, clear=False)
        if root is None:
            log_event(f"人机验证第{attempt}/{max_attempts}次：dump失败")
            time.sleep(2.0)
            continue
        info = detect_captcha(root)
        if info is None:
            log_event(f"人机验证第{attempt}/{max_attempts}次：页面已无验证，视为通过")
            return True
        log_event(f"人机验证第{attempt}/{max_attempts}次：式子={info.expr} 答案={info.answer}")
        if info.input_xy is None or info.confirm_xy is None:
            log_event("人机验证：找不到输入框或确认按钮")
            time.sleep(2.0)
            continue
        time.sleep(1.5)
        prepare_input()
        time.sleep(0.8)
        tap(*info.input_xy)
        time.sleep(1.5)
        input_captcha_answer(info.answer)
        time.sleep(1.8)
        tap(*info.confirm_xy)
        time.sleep(2.5)
        root2 = dump_root(retry=2, clear=False)
        if root2 is None:
            time.sleep(2.0)
            continue
        if detect_captcha(root2) is None:
            log_event("人机验证已通过，继续自动跑局")
            return True
        log_event("人机验证仍在，准备重试")
        time.sleep(2.0)
    return False



def is_upgrade_dialog(root: ET.Element) -> bool:
    """作业帮升级/更新提示：标题含升级或新版本，并有确定类按钮。"""
    texts = [t for t, _ in all_texts(root) if t]
    if not any(t == "确定" or t == "立即升级" for t in texts):
        return False
    keys = ("升级", "发现新版本", "版本更新", "有新版本", "新版本")
    return any(any(k in t for k in keys) for t in texts)



RANK_NAMES = ("青铜", "白银", "黄金", "铂金", "钻石", "星耀", "王者")


def detect_rank_up(root: ET.Element) -> tuple[int, int] | None:
    """口算段位升级弹窗：含段位相关文案 + 确定按钮时返回确定坐标。"""
    if detect_captcha(root) is not None:
        return None
    texts = all_texts(root)
    joined = " ".join(t for t, _ in texts if t)
    # 结算页常有「荣耀钻石」，需剔除后再匹配段位名，避免误点
    cleaned = joined.replace("荣耀钻石", "").replace("荣耀值", "")
    rankish = any(k in cleaned for k in ("段位", "升段", "晋级", "升到", "晋升")) or any(
        name in cleaned for name in RANK_NAMES
    )
    if not rankish:
        return None
    for t, b in texts:
        if t == "确定":
            xy = bounds_center(b)
            if xy:
                return xy
    for n in root.iter("node"):
        if (n.attrib.get("text") or "").strip() != "确定":
            continue
        xy = bounds_center(n.attrib.get("bounds") or "")
        if xy:
            return xy
    return None


def handle_rank_up(root: ET.Element) -> bool:
    xy = detect_rank_up(root)
    if xy is None:
        return False
    log_event("检测到口算段位升级弹窗，点击确定")
    prepare_input()
    time.sleep(0.3)
    tap(*xy)
    time.sleep(1.0)
    return True


def is_normal_wait_ui(root: ET.Element) -> bool:
    if detect_rank_up(root) is not None:
        return True
    texts = [t for t, _ in all_texts(root)]
    if any(t in texts for t in ("继续PK", "口算PK", "比大小", "比大小PK", "全部", "我知道了", "立即升级")):
        return True
    if any("手写" in t or "匹配" in t or t == "vs" or "题目数量" in t for t in texts):
        return True
    return False


def run_automation(args) -> None:
    rounds = 0
    last_state = None
    deadline = time.monotonic() + args.timeout
    failures = 0
    read_fail_streak = 0
    captcha_shot_done = False
    in_round = False
    last_navigation = 0.0
    submitted = 0
    batch_plan = []
    batch_attempts = 0

    def maybe_handle_captcha_from_streak(root_hint: ET.Element | None = None) -> bool:
        nonlocal read_fail_streak, captcha_shot_done, deadline
        if read_fail_streak < 2:
            return False
        if not captcha_shot_done:
            save_captcha_screenshot(args.log_dir)
            captcha_shot_done = True
        root = root_hint if root_hint is not None else dump_root(retry=2, clear=False)
        if root is None:
            return False
        if detect_captcha(root) is None:
            return False
        log_event("连续读题失败后检测到人机验证，开始自动处理")
        if not solve_captcha_math(args.log_dir, max_attempts=3):
            raise RuntimeError("人机验证自动处理失败，已停止以免误点")
        read_fail_streak = 0
        captcha_shot_done = False
        deadline = time.monotonic() + args.timeout
        return True

    while True:
        poll_pause_toggle()
        if _paused:
            time.sleep(0.1)
            deadline = time.monotonic() + args.timeout
            continue
        if time.monotonic() >= deadline:
            raise TimeoutError("页面未变化或输入未被接受，超时停止")
        root = dump_root(retry=1)
        if root is None:
            failures += 1
            read_fail_streak += 1
            log_event(f"读题失败(dump) streak={read_fail_streak}")
            if maybe_handle_captcha_from_streak(None):
                failures = 0
                continue
            if failures >= args.max_failures:
                raise RuntimeError("连续读取界面失败")
            time.sleep(args.interval)
            continue
        failures = 0

        # 直接看到验证页：自动处理，不再抛错退出
        if detect_captcha(root) is not None:
            log_event("检测到人机验证页面，开始自动处理")
            if not captcha_shot_done:
                save_captcha_screenshot(args.log_dir)
                captcha_shot_done = True
            if not solve_captcha_math(args.log_dir, max_attempts=3):
                raise RuntimeError("人机验证自动处理失败，已停止以免误点")
            read_fail_streak = 0
            captcha_shot_done = False
            deadline = time.monotonic() + args.timeout
            continue

        if handle_rank_up(root):
            deadline = time.monotonic() + args.timeout
            continue
        if has_text(root, "我知道了"):
            click_text(root, "我知道了")
            continue
        if is_upgrade_dialog(root):
            log_event("检测到升级弹窗，点击确定")
            if not click_text(root, "确定"):
                if not click_text(root, "立即升级"):
                    log_event("升级弹窗未找到可点按钮")
            deadline = time.monotonic() + args.timeout
            time.sleep(1.0)
            continue
        if has_text(root, "继续PK"):
            read_fail_streak = 0
            captcha_shot_done = False
            if in_round:
                rounds += 1
                summary = [t for t,b in all_texts(root) if "正确率" in t or "用时" in t]
                log_event(f"结算核验: {summary}")
                log_event(f"第 {rounds} 局已结算，脚本提交 {submitted} 题；提交成功不等于应用判题正确")
                if submitted < 10:
                    log_event("本局不足10次提交，可能提前结算或中途加入")
                in_round = False
                if args.rounds and rounds >= args.rounds:
                    return
            if time.monotonic() - last_navigation > 2:
                click_text(root, "继续PK")
                last_navigation = time.monotonic()
                last_state = None
                submitted = 0
                batch_plan = []
                batch_attempts = 0
                deadline = time.monotonic() + args.timeout
        else:
            q = current_question(root)
            done = progress_done(root)
            if q is not None:
                read_fail_streak = 0
                captcha_shot_done = False
            if args.strategy == "batch" and args.mode == "auto":
                if in_round and done is not None and 0 <= done < 10:
                    if batch_attempts >= 2:
                        raise RuntimeError(f"应用只接受到 {done}/10，补答后仍未完成，请增大 --answer-gap")
                    batch_attempts += 1
                    log_event(f"检测到漏答，按进度{done}/10重新同步剩余题目，第{batch_attempts}次补答")
                    send_batch(batch_plan[done:], max(args.answer_gap, 0.15))
                    deadline = time.monotonic() + args.timeout
                    time.sleep(1.0)
                    continue
                if not in_round and q is not None:
                    qs = batch_questions(root)
                    if done != 0 or len(qs) != 10:
                        raise RuntimeError(f"高速模式需从0/10开始且一次识别到完整10题，目前进度={done}，题数={len(qs)}；可用 --strategy checked 逐题运行")
                    answers = [judge(a,b) for a,b in qs]
                    if "=" in answers:
                        raise ValueError("等号题不支持原笔画，停止")
                    log_event(f"预读题序: {qs}，预计算答案: {answers}")
                    sender = prepare_batch_sender(qs, args.answer_gap)
                    try:
                        signal_time = wait_start(root, args.timeout, args.start_grace)
                    except Exception:
                        sender[0].terminate()
                        raise
                    if args.ready_delay:
                        time.sleep(args.ready_delay)
                    send_batch(qs, args.answer_gap, prepared_sender=sender, signal_time=signal_time)
                    batch_plan = qs
                    submitted = len(qs)
                    in_round = True
                    deadline = time.monotonic() + args.timeout
                    time.sleep(1.0)
            elif q is not None and done != 10:
                state = (q, done)
                if state != last_state:
                    op = judge(*q)
                    if op == "=":
                        raise ValueError("相等题目不支持原笔画")
                    if args.mode == "manual":
                        wait_arrow_command()
                    answer_checked(op)
                    submitted += 1
                    log_event(f"逐题提交: 进度={done}, {q[0]} {op} {q[1]}")
                    last_state = state
                    in_round = True
                    deadline = time.monotonic() + args.timeout
            navigated = False
            if q is None and not in_round and time.monotonic() - last_navigation > 2:
                for name in ("口算PK", "比大小", "比大小PK", "全部"):
                    if click_text(root, name):
                        last_navigation = time.monotonic()
                        navigated = True
                        break
            if q is None and not in_round and not navigated and not is_normal_wait_ui(root):
                read_fail_streak += 1
                log_event(f"读题失败(无题) streak={read_fail_streak}")
                if maybe_handle_captcha_from_streak(root):
                    continue
        time.sleep(args.interval)


def main() -> int:
    global ADB, LDCONSOLE, PACKAGE, CX, CY, SWIPE_MS, SERIAL, TMP_XML
    ap = argparse.ArgumentParser(description="作业帮比大小：自动识别、判断并逐题作答；空格暂停，Ctrl+C停止")
    ap.add_argument("--mode", choices=("auto", "manual"), default="auto")
    ap.add_argument("--strategy", choices=("batch", "checked"), default="batch", help="batch整局连续提交；checked逐题确认")
    ap.add_argument("--ready-delay", type=float, default=0.0, help="确认倒计时结束后额外等待秒数")
    ap.add_argument("--start-grace", type=float, default=0.04, help="检测开局后额外保护秒数")
    ap.add_argument("--answer-gap", type=float, default=0.008, help="批量题间等待秒数，过快漏题时增大")
    ap.add_argument("--once", action="store_true", help="完成一局后停止")
    ap.add_argument("--rounds", type=int, default=0, help="局数，0为持续运行（默认）")
    ap.add_argument("--adb", default=ADB)
    ap.add_argument("--ldconsole", default=LDCONSOLE)
    ap.add_argument("--serial", help="ADB设备序列号，多设备时必须指定")
    ap.add_argument("--package", default=PACKAGE)
    ap.add_argument("--center", nargs=2, type=int, default=(CX, CY), metavar=("X", "Y"))
    ap.add_argument("--swipe-ms", type=int, default=SWIPE_MS)
    ap.add_argument("--interval", type=float, default=0.05)
    ap.add_argument("--timeout", type=float, default=45)
    ap.add_argument("--max-failures", type=int, default=5)
    ap.add_argument("--log-dir", type=Path, default=Path(__file__).resolve().parent / "zyb_logs")
    ap.add_argument("--check", action="store_true", help="只检查ADB、设备和界面，不启动应用、不点击或作答")
    args = ap.parse_args()
    if args.ready_delay < 0 or args.start_grace < 0 or args.answer_gap < 0 or args.rounds < 0 or min(args.interval, args.timeout, args.swipe_ms, args.max_failures) <= 0:
        ap.error("局数必须非负，其余时间和失败次数必须大于0")
    if args.once:
        args.rounds = 1
    ADB, LDCONSOLE, PACKAGE, SERIAL = args.adb, args.ldconsole, args.package, args.serial
    CX, CY = args.center
    SWIPE_MS = args.swipe_ms
    args.log_dir.mkdir(parents=True, exist_ok=True)
    TMP_XML = args.log_dir / f"ui_{__import__('os').getpid()}.xml"
    file_handler = RotatingFileHandler(args.log_dir / "zyb.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=[file_handler, logging.StreamHandler()])
    try:
        if not Path(ADB).is_file():
            raise FileNotFoundError(f"ADB不存在: {ADB}")
        if not args.check and not ensure_ldplayer():
            raise RuntimeError("模拟器未连接")
        devices = adb(["devices"])
        if devices is None:
            raise RuntimeError("无法读取设备列表")
        online = [line.split()[0] for line in devices.stdout.decode().splitlines()[1:] if len(line.split()) >= 2 and line.split()[1] == "device"]
        if (SERIAL and SERIAL not in online) or (not SERIAL and len(online) != 1):
            raise RuntimeError(f"请连接唯一设备或用 --serial 指定设备；在线设备: {online}")
        SERIAL = SERIAL or online[0]
        log_event(f"设备: {SERIAL}，模式: {args.mode}")
        if args.check:
            root = dump_root(retry=1)
            if root is None:
                raise RuntimeError("设备已连接，但界面读取失败")
            log_event(f"检查通过：当前题={current_question(root)}，进度={progress_done(root)}")
            return 0
        ensure_app()
        run_automation(args)
        return 0
    except KeyboardInterrupt:
        log_event("用户停止")
        return 130
    except Exception:
        LOGGER.exception("运行停止")
        return 1
    finally:
        if _adb_shell is not None:
            _adb_shell.terminate()
        log_event("日志目录: " + str(args.log_dir.resolve()))


if __name__ == "__main__":
    sys.exit(main())

