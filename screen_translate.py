# -*- coding: utf-8 -*-
"""
屏译 —— 屏幕英文实时翻译（英 → 中）

流程：框选屏幕区域 → 定时截图 → 本地 OCR 取英文 → 必应翻译 → 悬浮窗显示
依赖：vendor/ 目录下的 pillow、rapidocr_onnxruntime（随程序附带）
作者：小鲤
"""
import os
import sys
import json
import time
import queue
import threading
import traceback
import urllib.parse
import urllib.request
import http.cookiejar
import difflib

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BASE, "vendor"))

import tkinter as tk
from tkinter import font as tkfont, messagebox

CONFIG_PATH = os.path.join(BASE, "config.json")
DEFAULT_CONFIG = {
    "region": None,            # [x1, y1, x2, y2]
    "interval": 0.6,           # 截图间隔（秒）
    "font_size": 22,
    "alpha": 0.82,
    "width": 900,              # 悬浮窗宽度（fixed_width=false 时是上限）
    "fixed_width": False,      # 拖过右下角把手后变 True：宽度固定，只自动调高
    "engine": "bing",          # bing / mymemory
    "show_source": True,       # 是否附上英文原文
}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception:
            pass
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


LOG_PATH = os.path.join(BASE, "log.txt")


def log(*parts):
    """把关键步骤写进 log.txt，出问题时能查到卡在哪一步。"""
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 300_000:
            os.replace(LOG_PATH, LOG_PATH + ".old")
        line = "%s  %s\n" % (time.strftime("%m-%d %H:%M:%S"),
                             " ".join(str(p) for p in parts))
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


# --------------------------------------------------------------------------
# 翻译
# --------------------------------------------------------------------------
class Translator:
    """必应免费接口优先，失败退到 MyMemory"""

    def __init__(self):
        self.cj = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cj))
        self.token = None
        self.key = None
        self.ig = None
        self.token_at = 0.0
        self.cache = {}
        self.lock = threading.Lock()

    def _fetch(self, url, data=None, headers=None):
        req = urllib.request.Request(url, data=data, headers=headers or {})
        req.add_header("User-Agent", UA)
        with self.opener.open(req, timeout=10) as r:
            return r.read()

    def _refresh_token(self):
        raw = self._fetch("https://cn.bing.com/translator?from=en&to=zh-Hans")
        html = raw.decode("utf-8", "ignore")
        import re
        m = re.search(r'params_AbusePreventionHelper\s*=\s*\[(\d+),\s*"([^"]+)"', html)
        ig = re.search(r'IG:"([^"]+)"', html)
        if not m or not ig:
            raise RuntimeError("取不到必应 token")
        self.key, self.token = m.group(1), m.group(2)
        self.ig = ig.group(1)
        self.token_at = time.time()

    def _bing(self, text):
        if not self.token or time.time() - self.token_at > 600:
            self._refresh_token()
        url = ("https://cn.bing.com/ttranslatev3?isVertical=1&&IG=%s&IID=translator.5028.1"
               % self.ig)
        form = urllib.parse.urlencode({
            "fromLang": "en", "to": "zh-Hans", "text": text,
            "token": self.token, "key": self.key,
        }).encode()
        raw = self._fetch(url, data=form, headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": "https://cn.bing.com/translator",
            "Origin": "https://cn.bing.com",
        })
        data = json.loads(raw.decode("utf-8", "replace"))
        if isinstance(data, dict) and data.get("statusCode"):
            raise RuntimeError("bing status %s" % data.get("statusCode"))
        return data[0]["translations"][0]["text"]

    def _mymemory(self, text):
        url = ("https://api.mymemory.translated.net/get?q=%s&langpair=en|zh-CN"
               % urllib.parse.quote(text[:480]))
        raw = self._fetch(url)
        data = json.loads(raw.decode("utf-8", "replace"))
        return data["responseData"]["translatedText"]

    def translate(self, text, engine="bing"):
        text = text.strip()
        if not text:
            return ""
        with self.lock:
            if text in self.cache:
                return self.cache[text]
        out = None
        last_err = None
        order = ["bing", "mymemory"] if engine == "bing" else ["mymemory", "bing"]
        for name in order:
            try:
                out = self._bing(text) if name == "bing" else self._mymemory(text)
                if out and out.strip():
                    break
            except Exception as e:
                last_err = e
                self.token = None
                out = None
        if not out:
            raise RuntimeError(str(last_err))
        with self.lock:
            if len(self.cache) > 500:
                self.cache.clear()
            self.cache[text] = out
        return out


def _is_net_error(e):
    """域名解析/连接类错误 = 网络问题，而不是接口本身出错。"""
    s = repr(e).lower()
    return any(k in s for k in ("getaddrinfo", "temporary failure", "name or service",
                                "connection", "timed out", "timeout", "网络",
                                "unreachable", "reset by peer", "ssl"))


# --------------------------------------------------------------------------
# OCR
# --------------------------------------------------------------------------
def clean_lines(result):
    """把 OCR 结果整理成英文句子列表"""
    if not result:
        return []
    rows = []
    for box, text, score in result:
        if score is not None and score < 0.55:
            continue
        t = (text or "").strip()
        if not t:
            continue
        letters = sum(c.isascii() and c.isalpha() for c in t)
        if letters < max(3, len(t) * 0.45):
            continue
        ys = [p[1] for p in box]
        xs = [p[0] for p in box]
        rows.append((sum(ys) / len(ys), sum(xs) / len(xs), t))
    rows.sort(key=lambda r: (round(r[0] / 12), r[1]))
    out, seen = [], set()
    for _, _, t in rows:
        t = " ".join(t.split())
        if t.lower() in seen:
            continue
        seen.add(t.lower())
        out.append(t)
    return out


# --------------------------------------------------------------------------
# 浮层：选区域
# --------------------------------------------------------------------------
class RegionSelector:
    def __init__(self, root, callback):
        self.callback = callback
        self.top = tk.Toplevel(root)
        self.top.attributes("-fullscreen", True)
        self.top.attributes("-alpha", 0.28)
        self.top.attributes("-topmost", True)
        self.top.configure(bg="black", cursor="crosshair")
        self.canvas = tk.Canvas(self.top, bg="black", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.create_text(
            self.top.winfo_screenwidth() // 2, 40,
            text="按住鼠标拖出要翻译的区域，松开确认（Esc 取消）",
            fill="white", font=("Microsoft YaHei UI", 20))
        self.start = None
        self.rect = None
        self.size_text = None
        self.canvas.bind("<ButtonPress-1>", self._down)
        self.canvas.bind("<B1-Motion>", self._move)
        self.canvas.bind("<ButtonRelease-1>", self._up)
        self.top.bind("<Escape>", lambda e: self.top.destroy())

    def _down(self, e):
        self.start = (e.x_root, e.y_root)
        if self.rect:
            self.canvas.delete(self.rect)
        self.rect = self.canvas.create_rectangle(
            e.x, e.y, e.x, e.y, outline="#4fc3f7", width=3)
        self.size_text = self.canvas.create_text(
            e.x + 12, e.y + 18, anchor="w", text="0 × 0",
            fill="#ffd54f", font=("Consolas", 15, "bold"))

    def _move(self, e):
        if self.start and self.rect:
            ox, oy = self.top.winfo_rootx(), self.top.winfo_rooty()
            sx, sy = self.start[0] - ox, self.start[1] - oy
            self.canvas.coords(self.rect, sx, sy, e.x, e.y)
            w = abs(e.x - sx)
            h = abs(e.y - sy)
            self.canvas.coords(self.size_text, e.x + 12, e.y + 18)
            self.canvas.itemconfig(self.size_text,
                                   text="%d × %d" % (w, h),
                                   fill="#ffd54f" if h >= 30 else "#ff8a80")

    def _up(self, e):
        if not self.start:
            return
        x1, y1 = self.start
        x2, y2 = e.x_root, e.y_root
        x1, x2 = sorted((x1, x2))
        y1, y2 = sorted((y1, y2))
        self.top.destroy()
        if x2 - x1 < 60 or y2 - y1 < 24:
            messagebox.showinfo(
                "屏译", "框太小了（%d × %d）。\n\n请按住左键拖出一整条字幕的高度"
                        "（一般是 60~200 像素高、几百像素宽），再松开。" % (x2 - x1, y2 - y1))
            return
        self.callback([x1, y1, x2, y2])


# --------------------------------------------------------------------------
# 浮层：译文窗口
# --------------------------------------------------------------------------
class ResultWindow:
    def __init__(self, root, cfg, on_font=None):
        self.cfg = cfg
        self.on_font = on_font
        self.top = tk.Toplevel(root)
        self.top.overrideredirect(True)
        self.top.attributes("-topmost", True)
        self.top.attributes("-alpha", cfg["alpha"])
        self.top.configure(bg="#101418")
        self.rect_xywh = (0, 0, 0, 0)
        self.frame = tk.Frame(self.top, bg="#101418", padx=18, pady=10)
        self.frame.pack()
        self.font = tkfont.Font(family="Microsoft YaHei UI", size=cfg["font_size"])
        self.label = tk.Label(self.frame, text="等待翻译…", font=self.font,
                              fg="#f5f7fa", bg="#101418",
                              wraplength=cfg["width"], justify="left")
        self.label.pack(anchor="w")
        self.source = tk.Label(self.frame, text="", font=("Consolas", 11),
                               fg="#7c8a99", bg="#101418",
                               wraplength=cfg["width"], justify="left")
        self.top.geometry("+%d+%d" % (300, 780))
        for w in (self.top, self.frame, self.label, self.source):
            w.bind("<Button-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)
            w.bind("<MouseWheel>", self._wheel)

        # 右下角的拖拽把手：左右拖 = 调宽度
        self.grip = tk.Label(self.top, text="◢", fg="#4fc3f7", bg="#101418",
                             font=("Consolas", 12), cursor="size_nw_se")
        self.grip.place(relx=1.0, rely=1.0, anchor="se", x=-2, y=-1)
        self.grip.bind("<ButtonPress-1>", self._grip_down)
        self.grip.bind("<B1-Motion>", self._grip_move)
        self.grip.bind("<ButtonRelease-1>", self._grip_up)
        self.grip.bind("<Double-Button-1>", self._grip_reset)
        self.grip.bind("<MouseWheel>", self._wheel)

        self._dx = self._dy = 0
        self._gw = self._gx = 0
        self.place_bottom_center()

    # ---------------- 调宽度 / 调字号 ----------------
    def _grip_down(self, e):
        self._gw = self.top.winfo_width()
        self._gx = e.x_root
        return "break"

    def _grip_move(self, e):
        w = max(220, min(1800, self._gw + (e.x_root - self._gx)))
        self.cfg["width"] = w - 40
        self.cfg["fixed_width"] = True
        self.label.config(wraplength=self.cfg["width"])
        self.source.config(wraplength=self.cfg["width"])
        self.top.geometry("%dx%d" % (w, self.top.winfo_height()))
        self.sync_rect()
        return "break"

    def _grip_reset(self, e):
        """双击把手：恢复自动宽度（跟着文字长短伸缩）。"""
        self.cfg["fixed_width"] = False
        save_config(self.cfg)
        self.place_keep_pos()
        return "break"

    def _grip_up(self, e):
        self.place_keep_pos()
        save_config(self.cfg)
        return "break"

    def _wheel(self, e):
        step = 2 if e.delta > 0 else -2
        size = max(10, min(72, int(self.cfg["font_size"]) + step))
        if size == self.cfg["font_size"]:
            return "break"
        self.cfg["font_size"] = size
        self.font.configure(size=size)
        save_config(self.cfg)
        if self.on_font:
            self.on_font(size)
        self.place_keep_pos()
        return "break"

    def place_keep_pos(self):
        """重新按内容算高度，位置不动。"""
        self.top.update_idletasks()
        w = self.top.winfo_width()
        h = max(int(self.top.winfo_reqheight()), 48)
        self.top.geometry("%dx%d" % (w, h))
        self.sync_rect()

    def place_bottom_center(self):
        self.top.update_idletasks()
        w = self.top.winfo_width()
        sw = self.top.winfo_screenwidth()
        sh = self.top.winfo_screenheight()
        self.top.geometry("+%d+%d" % (max(0, (sw - w) // 2), sh - 200))
        self.sync_rect()

    def sync_rect(self):
        """记住窗口在屏幕上的位置，供截图时挖掉，防止把自己的字再认一遍。"""
        try:
            self.top.update_idletasks()
            self.rect_xywh = (self.top.winfo_rootx(), self.top.winfo_rooty(),
                              self.top.winfo_width(), self.top.winfo_height())
        except Exception:
            pass

    def _drag_start(self, e):
        self._dx, self._dy = e.x_root - self.top.winfo_x(), e.y_root - self.top.winfo_y()

    def _drag_move(self, e):
        self.top.geometry("+%d+%d" % (e.x_root - self._dx, e.y_root - self._dy))
        self.sync_rect()

    def set_hint(self, text):
        """只改下面那行小字，不动主译文。"""
        self.source.config(text=text, fg="#ffb74d")
        self.source.pack(anchor="w", pady=(4, 0))
        self.top.update_idletasks()
        self.place_keep_pos()

    def set_text(self, zh, en="", warn=False):
        self.source.config(fg="#7c8a99")
        self.label.config(text=zh or "…", fg="#ffb74d" if warn else "#f5f7fa")
        if en and self.cfg.get("show_source"):
            self.source.config(text=en)
            self.source.pack(anchor="w", pady=(4, 0))
        else:
            self.source.pack_forget()
        self.top.update_idletasks()
        if self.cfg.get("fixed_width"):            # 手动拖过宽度：就按这个宽度排版
            w = int(self.cfg["width"]) + 40
        else:
            w = min(self.cfg["width"] + 40, max(self.label.winfo_reqwidth(),
                                                self.source.winfo_reqwidth()) + 40)
        w = max(int(w), 180)                      # 别缩成一个小方块
        h = max(int(self.top.winfo_reqheight()), 56)
        self.top.geometry("%dx%d" % (w, h))
        self.sync_rect()


# --------------------------------------------------------------------------
# 主程序
# --------------------------------------------------------------------------
class App:
    def __init__(self):
        self.cfg = load_config()
        self.root = tk.Tk()
        self.root.title("屏译 · 屏幕实时翻译")
        self.root.geometry("360x230+60+60")
        self.root.attributes("-topmost", True)
        self.root.configure(bg="#1b1f24")

        self.msg = queue.Queue()
        self.running = threading.Event()
        self.worker = None
        self.ocr = None
        self.panel_rect = (0, 0, 0, 0)
        self.gen = 0
        self.translator = Translator()

        tk.Label(self.root, text="屏译", font=("Microsoft YaHei UI", 16, "bold"),
                 fg="#4fc3f7", bg="#1b1f24").pack(pady=(12, 0))
        self.status = tk.Label(self.root, text="未选择区域", fg="#9aa7b4", bg="#1b1f24",
                               font=("Microsoft YaHei UI", 10), wraplength=320)
        self.status.pack(pady=4)

        bar = tk.Frame(self.root, bg="#1b1f24")
        bar.pack(pady=6)
        self.btn_region = tk.Button(bar, text="选区域", width=8, command=self.pick_region)
        self.btn_region.grid(row=0, column=0, padx=4)
        self.btn_start = tk.Button(bar, text="开始", width=8, command=self.toggle)
        self.btn_start.grid(row=0, column=1, padx=4)
        self.btn_stop = tk.Button(bar, text="清除", width=8, command=self.clear)
        self.btn_stop.grid(row=0, column=2, padx=4)
        self.btn_test = tk.Button(bar, text="测一下", width=8, command=self.test_once)
        self.btn_test.grid(row=0, column=3, padx=4)

        opts = tk.Frame(self.root, bg="#1b1f24")
        opts.pack()
        tk.Label(opts, text="字号", fg="#9aa7b4", bg="#1b1f24").grid(row=0, column=0)
        self.size = tk.IntVar(value=self.cfg["font_size"])
        tk.Spinbox(opts, from_=12, to=48, width=4, textvariable=self.size,
                   command=self.apply_size).grid(row=0, column=1, padx=(2, 12))
        tk.Label(opts, text="速度", fg="#9aa7b4", bg="#1b1f24").grid(row=0, column=2)
        self.iv = tk.DoubleVar(value=self.cfg["interval"])
        tk.Spinbox(opts, from_=0.3, to=3.0, increment=0.1, width=4,
                   textvariable=self.iv, command=self.apply_speed).grid(row=0, column=3, padx=2)

        self.result = ResultWindow(self.root, self.cfg, on_font=self.sync_size)
        self.result.set_text("点「开始」后这里出译文")

        self.root.after(100, self.poll)
        self.root.protocol("WM_DELETE_WINDOW", self.quit)

    # ---------------- UI 动作 ----------------
    def apply_size(self):
        self.cfg["font_size"] = int(self.size.get())
        self.result.font.configure(size=self.cfg["font_size"])
        self.result.place_keep_pos()
        save_config(self.cfg)

    def sync_size(self, size):
        """浮窗上滚轮改字号后，把面板里的数字同步过来。"""
        try:
            self.size.set(int(size))
        except Exception:
            pass

    def apply_speed(self):
        self.cfg["interval"] = float(self.iv.get())
        save_config(self.cfg)

    def pick_region(self):
        self.stop()
        RegionSelector(self.root, self.on_region)

    def on_region(self, region):
        self.cfg["region"] = region
        save_config(self.cfg)
        self.status.config(text="区域: %s" % (region,))

    def clear(self):
        self.stop()
        self.result.set_text("")

    # ---------------- 单次测试 ----------------
    def test_once(self):
        """拍一帧、认一次、翻一次，把每一步结果直接弹出来。"""
        if not self.cfg.get("region"):
            messagebox.showinfo("屏译", "先点「选区域」框出英文所在的位置")
            return
        self.status.config(text="测试中…")
        threading.Thread(target=self._test_worker, daemon=True).start()

    def _test_worker(self):
        import numpy as np
        from PIL import ImageGrab
        try:
            x1, y1, x2, y2 = self.cfg["region"]
            log("测试区域:", self.cfg["region"])
            img = ImageGrab.grab(bbox=(x1, y1, x2, y2), all_screens=True).convert("RGB")
            w, h = img.size
            if h < 140:
                scale = min(3.0, 140 / max(h, 1))
                img = img.resize((int(w * scale), int(h * scale)))
            elif w > 1280:
                img = img.resize((1280, max(1, int(h * 1280 / w))))
            img.save(os.path.join(BASE, "_last_region.png"))
            log("测试截图尺寸:", img.size)
            if np.asarray(img.convert("L"), dtype=np.int16).std() < 3:
                self._popup("截到的是纯色（全黑或全白）画面。\n\n"
                            "常见原因：游戏在全屏独占模式，系统截不到画面。\n"
                            "把游戏改成「无边框窗口 / 窗口化」再试。\n\n"
                            "截图已存 _last_region.png，可以打开确认。")
                return

            if self.ocr is None:
                from rapidocr_onnxruntime import RapidOCR
                self.ocr = RapidOCR()
            t0 = time.time()
            res, _ = self.ocr(np.asarray(img)[:, :, ::-1], use_cls=False)
            lines = clean_lines(res)
            text = " ".join(lines).strip()
            ocr_info = "识别用时 %.1fs，%d 行\n%s" % (time.time() - t0, len(lines), text[:300])
            log("测试 OCR:", ocr_info.replace("\n", " | "))
            if not text:
                self._popup("测试结果：没认出字\n\n"
                            "截图已存为 _last_region.png，打开看看框对不对；\n"
                            "框太小/字太小时把它放大一点。")
                return

            t1 = time.time()
            try:
                zh = self.translator.translate(text, self.cfg["engine"])
            except Exception as e:
                log("测试翻译失败:", repr(e))
                self._popup("认出来的英文：\n%s\n\n翻译失败了（%.1fs）：\n%s\n\n"
                            "说明网络连不上翻译接口，换个网或等会儿再试。"
                            % (text[:300], time.time() - t1, e))
                return
            log("测试译文:", zh)
            self._popup("英文：\n%s\n\n中文：\n%s\n\n总用时 %.1fs"
                        % (text[:300], zh, time.time() - t0))
        except Exception as e:
            log("测试异常:", repr(e))
            self._popup("测试出错：\n%s" % e)

    def _popup(self, text):
        self.root.after(0, lambda: (self.status.config(text="测试完成"),
                                    messagebox.showinfo("屏译 · 测试", text)))

    def toggle(self):
        if self.running.is_set():
            self.stop()
        else:
            self.start()

    def start(self):
        if not self.cfg.get("region"):
            messagebox.showinfo("屏译", "先点「选区域」框出英文所在的位置")
            return
        self.running.set()
        self.btn_start.config(text="暂停")
        self.status.config(text="开始，等 1~2 秒出第一句…")
        self._avoid_overlap()
        self.gen += 1                      # 编号变了，旧线程会自己退出
        gen = self.gen
        log("开始监听 区域=%s" % (self.cfg.get("region"),))
        self.worker = threading.Thread(target=self.loop, args=(gen,), daemon=True)
        self.worker.start()

    def _avoid_overlap(self):
        """浮窗要是压在识别区上，自动挪到识别区上方。"""
        region = self.cfg.get("region")
        if not region:
            return
        x1, y1, x2, y2 = region
        self.result.sync_rect()
        rx, ry, rw, rh = self.result.rect_xywh
        if rw <= 1 or rh <= 1:
            return
        if rx < x2 and rx + rw > x1 and ry < y2 and ry + rh > y1:
            ny = max(0, y1 - rh - 16)
            nx = max(0, min(rx, self.root.winfo_screenwidth() - rw))
            self.result.top.geometry("+%d+%d" % (nx, ny))
            self.result.sync_rect()
            log("浮窗与识别区重叠，已自动移到上方")

    def stop(self):
        self.running.clear()
        self.btn_start.config(text="开始")

    def quit(self):
        self.running.clear()
        self.root.destroy()

    # ---------------- 后台循环 ----------------
    def loop(self, gen):
        from PIL import ImageGrab
        import numpy as np
        last_text = ""
        last_zh = ""
        last_sig = None
        fails = 0
        warned_overlap = False
        net_fails = 0
        recent = []
        while self.running.is_set() and gen == self.gen:
            try:
                if self.ocr is None:
                    self.msg.put(("status", "加载 OCR 引擎…（首次约 3 秒）"))
                    log("加载 OCR 引擎")
                    from rapidocr_onnxruntime import RapidOCR
                    self.ocr = RapidOCR()
                    log("OCR 引擎就绪")
                    self.msg.put(("status", "就绪，盯着屏幕中…"))

                region = self.cfg.get("region")
                if not region:
                    time.sleep(0.3)
                    continue
                x1, y1, x2, y2 = region
                img = ImageGrab.grab(bbox=(x1, y1, x2, y2), all_screens=True).convert("RGB")

                # 自己的窗口压在识别区上时，先把它涂黑，避免「翻 → 窗口变 → 再翻」死循环
                if self._mask_overlays(img, x1, y1, x2, y2) and not warned_overlap:
                    warned_overlap = True
                    self.msg.put(("status", "浮窗挡住了识别区，已自动忽略挡住的部分"))

                # 画面没变就不重复识别，省 CPU
                sig = np.asarray(img.convert("L").resize((48, 16)), dtype=np.int16)
                if last_sig is not None and np.abs(sig - last_sig).mean() < 4.0:
                    time.sleep(self.cfg["interval"])
                    continue
                last_sig = sig

                # 全黑/纯色 = 游戏全屏独占模式抓不到画面
                if np.asarray(img.convert("L"), dtype=np.int16).std() < 3:
                    self.msg.put(("placeholder", "截到的是纯色画面"))
                    self.msg.put(("status", "抓不到画面：游戏若为全屏独占，改成「无边框窗口」"))
                    time.sleep(1.0)
                    continue

                w, h = img.size
                if h < 140:                      # 太小的字幕条放大再识别
                    scale = min(3.0, 140 / max(h, 1))
                    img = img.resize((int(w * scale), int(h * scale)))
                elif w > 1280:                   # 太大反而慢
                    img = img.resize((1280, max(1, int(h * 1280 / w))))
                frame = np.asarray(img)[:, :, ::-1]

                t_ocr = time.time()
                res, _ = self.ocr(frame, use_cls=False)
                lines = clean_lines(res)
                text = " ".join(lines).strip()
                log("识别 %.1fs %dx%d 行=%d 文本=%r"
                    % (time.time() - t_ocr, img.width, img.height, len(lines), text[:80]))

                if not text:
                    fails += 1
                    if fails == 1:
                        self.msg.put(("status", "框里没认出英文（框小一点、对准字幕）"))
                        self.msg.put(("placeholder", "框里没认出字"))
                    time.sleep(self.cfg["interval"])
                    continue
                fails = 0

                norm = " ".join("".join(c.lower() if c.isalnum() else " "
                                        for c in text).split())
                if norm and any(difflib.SequenceMatcher(None, n, norm).ratio() > 0.85
                                for n in recent):
                    time.sleep(self.cfg["interval"])
                    continue
                recent.append(norm)
                del recent[:-6]

                self.msg.put(("status", "翻译中…"))
                self.msg.put(("placeholder", "翻译中…"))
                try:
                    zh = self.translator.translate(text, self.cfg["engine"])
                    net_fails = 0
                except Exception as e:
                    net_fails += 1
                    kind = "网络不通" if _is_net_error(e) else "翻译接口出错"
                    log("%s(%d):" % (kind, net_fails), repr(e))
                    if net_fails <= 1:            # 只提示一次，别刷屏
                        self.msg.put(("status", "%s，正在重试…" % kind))
                        self.msg.put(("hint", "⏳ %s，自动重试中（联网恢复后继续）" % kind))
                    time.sleep(min(1.0 * net_fails, 5.0))
                    continue

                last_text = text
                log("译文:", zh)
                if zh == last_zh:                 # 和上一句译文一样就不刷屏
                    time.sleep(self.cfg["interval"])
                    continue
                last_zh = zh
                self.msg.put(("text", (zh, text)))
                self.msg.put(("status", "已翻译（%d 行）" % len(lines)))
            except Exception as e:
                log("异常:", repr(e))
                traceback.print_exc()
                self.msg.put(("status", "出错: %s" % e))
                time.sleep(1.0)

    # ---------------- 把自己盖住的部分挖掉 ----------------
    def _mask_overlays(self, img, x1, y1, x2, y2):
        """截图里如果有自己的浮窗/控制面板，涂黑掉。
        否则浮窗上的英文原文会被再认一遍 → 无限重复翻译。"""
        from PIL import ImageDraw
        drawer = ImageDraw.Draw(img)
        hit = False
        for (rx, ry, rw, rh) in (self.result.rect_xywh, self.panel_rect):
            if rw <= 1 or rh <= 1:
                continue
            ix1, iy1 = max(x1, rx), max(y1, ry)
            ix2, iy2 = min(x2, rx + rw), min(y2, ry + rh)
            if ix2 > ix1 and iy2 > iy1:
                drawer.rectangle([ix1 - x1, iy1 - y1, ix2 - x1 - 1, iy2 - y1 - 1],
                                 fill=(0, 0, 0))
                hit = True
        return hit

    def poll(self):
        try:
            self.panel_rect = (self.root.winfo_rootx(), self.root.winfo_rooty(),
                               self.root.winfo_width(), self.root.winfo_height())
        except Exception:
            pass
        try:
            while True:
                kind, payload = self.msg.get_nowait()
                if kind == "text":
                    zh, en = payload
                    self.result.set_text(zh, en)
                elif kind == "status":
                    self.status.config(text=payload)
                elif kind == "placeholder":
                    self.result.set_text(payload, "", warn=True)
                elif kind == "warn":
                    self.result.set_text(payload, "", warn=True)
                elif kind == "hint":
                    self.result.set_hint(payload)
        except queue.Empty:
            pass
        self.root.after(120, self.poll)


def set_dpi_aware():
    """让窗口坐标 = 真实像素，否则 125%/150% 缩放时框选和截图会错位。"""
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)   # 每显示器 DPI 感知
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
        log("已开启 DPI 感知")
    except Exception as e:
        log("DPI 设置失败:", repr(e))


def main():
    set_dpi_aware()
    app = App()
    app.root.mainloop()


if __name__ == "__main__":
    main()
