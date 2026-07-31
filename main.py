#!/usr/bin/env python3
"""
土豆兄弟·一键托管工具 (Brotato Auto Helper)
=============================================
自动升级、拾取箱子、商店购买，全程托管。

用法：
    python main.py

功能：
    1. 框选任务区域
    2. 热键 F7 切换连续自动升级（默认值，可在设置中修改）
    3. 热键 F6 触发一次性升级

依赖安装：
    pip install -r requirements.txt
"""

import json
import os
import sys
import time
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
from threading import Thread, Event, Lock
from pathlib import Path
from datetime import datetime

import numpy as np
from PIL import Image
import mss
from pynput import keyboard, mouse
from pynput.keyboard import Key, Controller as KBController
from pynput.mouse import Button, Controller as MouseController

# ---------------------------------------------------------------------------
# 可选依赖检查
# ---------------------------------------------------------------------------

try:
    import cv2
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False


try:
    import easyocr
    HAS_EASYOCR = True
except ImportError:
    HAS_EASYOCR = False

# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).parent
CONFIG_PATH = SCRIPT_DIR / "config.json"


# ===================================================================
# ConfigManager — 配置读写
# ===================================================================

class ConfigManager:
    """管理 config.json 的加载与保存。"""

    DEFAULT = {
        "delay_between_upgrades_ms": 600,
        "upgrade_max": 0,  # 升级次数上限，0=不限制
        "upgrade_detect_region": {"left": 800, "top": 200, "width": 120, "height": 50},
        "upgrade_click_coord": None,  # 鼠标升级点击坐标，如 {"x": 960, "y": 540}；必须设置，否则无法升级
        "shortcuts_enabled": True,
        "shortcuts": {
            "stop": "<esc>",
            "toggle": "<f8>",
            "trigger_once": "<f7>",
            "test_crate": "t",
            "test_item": "y",
            "pause": "<f6>",
            "test_upgrade_detect": "u",
        },
        "font_size": 16,
        # 浮窗设置
        "overlay_enabled": True,
        "overlay_x": 20,
        "overlay_y": 0,
        "overlay_width": 340,
        "overlay_height": 120,
        "overlay_font_size": 13,
        "overlay_opacity": 0.84,
        "overlay_snap_margin": 20,
        # 拾取箱子设置
        "crate_enabled": True,
        "crate_auto_skip": False,
        "crate_region": {"left": 800, "top": 400, "width": 160, "height": 60},
        "item_region": {"left": 600, "top": 160, "width": 500, "height": 140},
        "desired_items": [],
        "crate_delay_ms": 800,
        # 商店购买设置
        "shop_enabled": True,
        "shop_auto_skip": False,
        "shop_target_item": "沙漏",
        "shop_secondary_items": ["水熊虫", "镜子"],
        "shop_max_refreshes": 2000,
        "shop_delay_ms": 1000,
        "shop_detect_region": {"left": 800, "top": 500, "width": 160, "height": 50},
        "shop_slot_regions": [
            {"left": 500, "top": 300, "width": 180, "height": 50},
            {"left": 700, "top": 300, "width": 180, "height": 50},
            {"left": 900, "top": 300, "width": 180, "height": 50},
            {"left": 1100, "top": 300, "width": 180, "height": 50},
        ],
        "shop_slot_buy_coords": [
            {"x": 590, "y": 400},
            {"x": 790, "y": 400},
            {"x": 990, "y": 400},
            {"x": 1190, "y": 400},
        ],
        "shop_refresh_coord": {"x": 960, "y": 600},
        "shop_leave_coord": {"x": 960, "y": 700},
    }

    # 已废弃的配置项（早期"数字识别"方案与旧版热键字段），加载时自动清除
    LEGACY_KEYS = (
        "upgrade_keys",
        "hotkey_toggle",
        "hotkey_trigger_once",
        "hotkey_stop",
        "region",
        "tesseract_path",
        "ocr_confidence_min",
        "preprocess_scale",
        "digit_templates",
    )

    def __init__(self, path: Path = CONFIG_PATH):
        self.path = path
        self.data = dict(self.DEFAULT)
        self.load()

    def load(self):
        if self.path.exists():
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                # 深度合并，保证新字段有默认值
                self._deep_merge(self.data, loaded)
                # 规范化快捷键（tkinter keysym → pynput Key.name）
                self._normalize_shortcuts()
                # 清除已废弃配置项
                for key in self.LEGACY_KEYS:
                    self.data.pop(key, None)
            except Exception as e:
                print(f"[警告] 加载配置失败: {e}，使用默认配置")

    def _normalize_shortcuts(self):
        """确保 config 中 shortcuts 的键名与 pynput 一致。"""
        mapping = {"<escape>": "<esc>", "<return>": "<enter>"}
        if "shortcuts" in self.data:
            for action, key in list(self.data["shortcuts"].items()):
                if key in mapping:
                    self.data["shortcuts"][action] = mapping[key]

    def save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"[错误] 保存配置失败: {e}")

    def get(self, *keys):
        """链式取值，例如 get("shortcuts", "stop")。"""
        val = self.data
        for k in keys:
            val = val[k]
        return val

    def set(self, *keys_and_value):
        """链式设值，最后一个参数是 value。例如 set("shortcuts", "stop", "<f8>")。"""
        *keys, value = keys_and_value
        d = self.data
        for k in keys[:-1]:
            d = d[k]
        d[keys[-1]] = value

    @staticmethod
    def _deep_merge(base, update):
        for k, v in update.items():
            if k in base and isinstance(base[k], dict) and isinstance(v, dict):
                ConfigManager._deep_merge(base[k], v)
            else:
                base[k] = v


# ===================================================================
# ScreenReader — 屏幕截图 + OCR 数字识别
# ===================================================================

class ScreenReader:
    """从指定屏幕区域截图，用多种策略进行 OCR 数字识别。"""

    def __init__(self, config: ConfigManager):
        self.config = config
        self.sct = mss.MSS()
        self._easyocr_reader = None  # 懒加载


    def _get_easyocr(self):
        """懒加载 EasyOCR reader。"""
        if self._easyocr_reader is None and HAS_EASYOCR:
            model_dir = getattr(sys, '_MEIPASS', None)
            if model_dir:
                model_dir = os.path.join(model_dir, 'EasyOCR', 'model')
            try:
                self._easyocr_reader = easyocr.Reader(
                    ['ch_sim', 'en'], gpu=False, verbose=False,
                    model_storage_directory=model_dir,
                    download_enabled=(model_dir is None)
                )
            except Exception as e:
                import tkinter.messagebox as _mb
                _mb.showerror("OCR 错误", f"Reader 创建失败:\n{e}")
        return self._easyocr_reader

    # ---- 通用区域 OCR（用于中文 / 道具名识别） ----

    def ocr_text(self, region: dict, lang: str = "chi_sim+eng") -> str:
        """对任意屏幕区域做中文 OCR。"""
        monitor = {
            "left": region["left"], "top": region["top"],
            "width": region["width"], "height": region["height"],
        }
        sct_img = self.sct.grab(monitor)
        bgr = np.array(sct_img, dtype=np.uint8)[:, :, :3]

        reader = self._get_easyocr()
        if reader is not None:
            try:
                results = reader.readtext(bgr, detail=0)
                text = " ".join(results).strip()
                return text
            except Exception:
                pass
        return ""

    def grab_region_image(self, region: dict) -> np.ndarray:
        """截取区域，返回 BGR 数组（供预览用）。"""
        monitor = {
            "left": region["left"], "top": region["top"],
            "width": region["width"], "height": region["height"],
        }
        sct_img = self.sct.grab(monitor)
        return np.array(sct_img, dtype=np.uint8)[:, :, :3]


# ===================================================================
# AutoUpgrader — 核心自动化逻辑
# ===================================================================

class AutoUpgrader:
    """管理自动升级的启动/停止与执行。"""

    def __init__(self, config: ConfigManager, log_callback=None, status_callback=None,
                 history_callback=None, history_clear_callback=None):
        self.config = config
        self.log = log_callback or print
        self.status = status_callback or (lambda t, c=None: None)
        self.history = history_callback or (lambda t, c=None: None)
        self.history_clear = history_clear_callback or (lambda: None)
        self.reader = ScreenReader(config)
        self.kb = KBController()
        self.mouse = MouseController()

        self._running = False
        self._stop_event = Event()
        self._worker_thread: Thread | None = None
        self._lock = Lock()
        self._shop_cooldown: float = 0  # 商店检测冷却时间戳
        self._general_pause = Event()  # 通用暂停（非升级环节可切换）
        self._current_phase = "idle"   # 当前阶段: idle/crate/upgrade/shop

    # ---- 属性 ----

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._running

    # ---- 可中断 sleep ----

    def _sleep_check(self, duration: float, tick: float = 0.05) -> bool:
        """
        分段 sleep，每 tick 秒检查 _stop_event 和 _general_pause。
        返回 True 表示被 _stop_event 中断。
        """
        elapsed = 0.0
        while elapsed < duration:
            if self._stop_event.is_set():
                return True
            while self._general_pause.is_set():
                if self._stop_event.is_set():
                    return True
                self.status("⏸ 已暂停", "#f39c12")
                time.sleep(0.1)
            chunk = min(tick, duration - elapsed)
            time.sleep(chunk)
            elapsed += chunk
        return False

    # ---- 界面检测 ----

    def _is_on_upgrade_screen(self) -> bool:
        """检测当前是否在升级界面（OCR 区域中含'升级'且不长于10字）。"""
        region = self.config.data.get("upgrade_detect_region")
        if not region:
            return False
        try:
            text = self.reader.ocr_text(region, lang="chi_sim+eng").strip()
            result = text in ("升级", "升级!")
            if not hasattr(self, '_last_upgrade_state') or self._last_upgrade_state != result:
                self._last_upgrade_state = result
                self.log(f"  [升级检测] OCR=[{text}] → {'升级界面' if result else '非升级界面'}")
            return result
        except Exception:
            return False

    # ---- 按键模拟 ----

    def _press_upgrade_once(self):
        """执行一次升级点击，点击后即将鼠标移走。"""
        coord = self.config.data.get("upgrade_click_coord")
        if not coord or not coord.get("x"):
            self.log("  ⚠ 未设置升级点击坐标，请在升级与操作设置中框选")
            return
        self.mouse.position = (coord["x"], coord["y"])
        self.mouse.click(Button.left, 1)
        self.mouse.position = (0, 0)

    # ---- 拾取箱子 ----

    def _handle_crate(self) -> dict | None:
        """
        检测并处理箱子拾取界面。
        返回 {"name": str, "picked": bool}；不在箱子界面返回 None。
        """
        # 通用暂停检查：若已暂停则阻塞直到恢复
        while self._general_pause.is_set():
            if self._stop_event.is_set():
                return None
            self.status("⏸ 已暂停", "#f39c12")
            time.sleep(0.1)
        if not self.config.data.get("crate_enabled", True):
            return None

        crate_region = self.config.data.get("crate_region")
        if not crate_region:
            return None

        # 检查是否在箱子界面
        crate_text = self.reader.ocr_text(crate_region, lang="chi_sim+eng")
        in_crate = "属性" in crate_text
        self.log(f"  [箱子检测] 区域OCR文本=[{crate_text}] 检测\"属性\"={'✓' if in_crate else '✗'}")

        if not in_crate:
            return None

        # 确认在箱子界面后，再次检查暂停（避免按暂停后仍多执行一次）
        while self._general_pause.is_set():
            if self._stop_event.is_set():
                return None
            self.status("⏸ 已暂停", "#f39c12")
            time.sleep(0.1)

        # 自动跳过：不检测道具内容，直接按 F 跳过
        if self.config.data.get("crate_auto_skip", False):
            self.log("  ⏭ 自动跳过所有物品（已开启自动跳过）")
            self.kb.press("f")
            self.kb.release("f")
            crate_delay = self.config.data.get("crate_delay_ms", 800) / 1000.0
            self._sleep_check(crate_delay)
            return {"name": "", "picked": False}

        # 识别道具名
        item_region = self.config.data.get("item_region")
        item_name = ""
        if item_region:
            raw_text = self.reader.ocr_text(item_region, lang="chi_sim+eng")
            lines = [l.strip() for l in raw_text.split('\n') if l.strip()]
            ui_keywords = ['属性', '生命', '攻击', '速度', '范围', '伤害', '暴击',
                          '护甲', '闪避', '收获', '幸运', '等级', '波次', '材料']
            item_lines = [
                l for l in lines
                if len(l) >= 2 and not l.isdigit()
                and not any(kw in l for kw in ui_keywords)
            ]
            item_name = item_lines[0] if item_lines else (lines[0] if lines else "")
            self.log(f"  [道具识别] 原始({len(raw_text)}字)=[{raw_text[:100]}]")
            self.log(f"  [道具识别] 提取名=[{item_name}]")

        # 匹配期望道具
        desired = self.config.get("desired_items")
        should_pick = False
        matched_item = ""
        if desired and item_name:
            for d in desired:
                d = d.strip()
                if d and d in item_name:
                    should_pick = True
                    matched_item = d
                    break

        # 按键前最后一次暂停检查
        while self._general_pause.is_set():
            if self._stop_event.is_set():
                return {"name": item_name, "picked": False}
            self.status("⏸ 已暂停", "#f39c12")
            time.sleep(0.1)

        if should_pick:
            self.status(f"🎁 拾取: {item_name}", "#4ecdc4")
            self.history(f"🎁 拾取 {item_name}", "#4ecdc4")
            self.log(f"  🎁 拾取道具: [{item_name}] (匹配 [{matched_item}]) → 按空格")
            self.kb.press(Key.space)
            self.kb.release(Key.space)
        else:
            self.status(f"🗑 跳过: {item_name or '未知道具'}", "#888888")
            reason = "未匹配期望列表" if item_name else "未识别到道具名"
            self.log(f"  🗑 跳过道具: [{item_name}] ({reason}) → 按 F")
            self.kb.press("f")
            self.kb.release("f")

        crate_delay = self.config.data.get("crate_delay_ms", 800) / 1000.0
        self._sleep_check(crate_delay)
        return {"name": item_name, "picked": should_pick}

    # ---- 商店购买 ----

    def _click_at(self, x: int, y: int):
        """移动鼠标到 (x, y) 并点击左键，点击后将鼠标移开防止误长按。"""
        self.mouse.position = (x, y)
        time.sleep(0.05)
        self.mouse.click(Button.left, 1)
        time.sleep(0.02)
        self.mouse.position = (0, 0)  # 移开鼠标，防止长按触发锁定

    def _wave_countdown(self, seconds: int = 60):
        """波次倒计时，支持暂停。"""
        for remaining in range(seconds, 0, -1):
            if self._stop_event.is_set():
                return
            while self._general_pause.is_set():
                if self._stop_event.is_set():
                    return
                self.status("⏸ 已暂停", "#f39c12")
                time.sleep(0.1)
            self.status(f"⏳ 波次倒计时 {remaining}s", "#3498db")
            time.sleep(1)
        self.history_clear()

    def _handle_shop(self, crate_results: list[dict]) -> bool:
        """
        商店购买逻辑。
        返回 True 表示已处理（点击了出发），False 表示不在商店或跳过。
        """
        # 通用暂停检查：若已暂停则阻塞直到恢复
        while self._general_pause.is_set():
            if self._stop_event.is_set():
                return False
            self.status("⏸ 已暂停", "#f39c12")
            time.sleep(0.1)
        if not self.config.data.get("shop_enabled", True):
            return False

        detect_region = self.config.data.get("shop_detect_region")
        if not detect_region:
            return False

        # 冷却期内跳过商店检测
        if time.time() < self._shop_cooldown:
            return False

        # 检测是否在商店界面（重试最多 3 次，等待画面过渡）
        in_shop = False
        shop_text = ""
        for attempt in range(3):
            time.sleep(0.5)
            # 每轮重试前检查暂停
            while self._general_pause.is_set():
                if self._stop_event.is_set():
                    return False
                self.status("⏸ 已暂停", "#f39c12")
                time.sleep(0.1)
            shop_text = self.reader.ocr_text(detect_region, lang="chi_sim+eng")
            in_shop = "商店" in shop_text
            self.log(f"  [商店检测 #{attempt+1}] OCR=[{shop_text}] 在商店={'✓' if in_shop else '✗'}")
            if in_shop:
                break
        if not in_shop:
            return False

        # 确认在商店后，再次检查暂停
        while self._general_pause.is_set():
            if self._stop_event.is_set():
                return False
            self.status("⏸ 已暂停", "#f39c12")
            time.sleep(0.1)

        # 自动跳过：不进购买环节，直接出发
        if self.config.data.get("shop_auto_skip", False):
            self.log("  ⏭ 自动跳过商店（已开启自动跳过）")
            leave = self.config.data.get("shop_leave_coord")
            if leave:
                self._click_at(leave["x"], leave["y"])
                self._sleep_check(1.0)
            self._wave_countdown()
            return True

        # 检查箱子阶段是否已拿到目标道具
        target = self.config.data.get("shop_target_item", "沙漏")
        crate_picked = [r["name"] for r in crate_results if r["picked"]]
        if target in crate_picked or any(target in name for name in crate_picked):
            self.log(f"  [商店] 已在箱子阶段获得 [{target}]，直接出发")
            leave = self.config.data.get("shop_leave_coord")
            if leave:
                self._click_at(leave["x"], leave["y"])
                self._sleep_check(1.0)
            self._wave_countdown()
            return True

        # 商店循环
        slot_regions = self.config.data.get("shop_slot_regions", [])
        buy_coords = self.config.data.get("shop_slot_buy_coords", [])
        refresh_coord = self.config.data.get("shop_refresh_coord")
        leave_coord = self.config.data.get("shop_leave_coord")
        secondary = self.config.data.get("shop_secondary_items", [])
        max_refreshes = self.config.data.get("shop_max_refreshes", 2000)
        delay = self.config.data.get("shop_delay_ms", 500) / 1000.0

        bought_secondary: list[str] = []

        for refresh_num in range(1, max_refreshes + 1):
            if self._stop_event.is_set():
                self.log("  [商店] 被中断")
                return False

            self.status(f"🛒 商店刷新 #{refresh_num}", "#9b59b6")
            slot_names: list[str] = []
            for i, region in enumerate(slot_regions):
                if i >= len(buy_coords):
                    break
                text = self.reader.ocr_text(region, lang="chi_sim+eng")
                slot_names.append(text)

            # 查找目标道具
            found_target_slot = -1
            found_secondary_slots: list[tuple[int, str]] = []
            self.log(f"  [商店 #{refresh_num}] 槽位OCR: {' | '.join(f'{i+1}=[{n[:20]}]' for i, n in enumerate(slot_names))}")
            for i, name in enumerate(slot_names):
                if target in name:
                    found_target_slot = i
                    self.log(f"  [商店 #{refresh_num}]   槽{i+1} 匹配目标 [{target}]")
                for item in secondary:
                    if item in name:
                        found_secondary_slots.append((i, item))
                        self.log(f"  [商店 #{refresh_num}]   槽{i+1} 匹配次级 [{item}]")

            if found_target_slot >= 0:
                self.status(f"🛒 购买 [{target}] 并出发", "#2ecc71")
                self.history(f"🛒 购 {target}", "#2ecc71")
                self.log(f"  [商店 #{refresh_num}] 发现 [{target}] 在槽位 {found_target_slot+1}，购买并出发")
                self._click_at(buy_coords[found_target_slot]["x"], buy_coords[found_target_slot]["y"])
                self._sleep_check(delay)
                self._click_at(leave_coord["x"], leave_coord["y"])
                self._sleep_check(delay)
                all_bought = [target] + [item for _, item in found_secondary_slots]
                self.log(f"  🛒 商店购买: {'、'.join(all_bought)}")
                self._wave_countdown()
                return True

            # 没有目标，检查次级道具
            for slot_i, item_name in found_secondary_slots:
                self.status(f"🛒 购买次级 [{item_name}]", "#9b59b6")
                self.history(f"🛒 购 {item_name}", "#9b59b6")
                self.log(f"  [商店 #{refresh_num}] 发现次级道具 [{item_name}] 在槽位 {slot_i+1}，购买")
                self._click_at(buy_coords[slot_i]["x"], buy_coords[slot_i]["y"])
                self._sleep_check(delay)
                bought_secondary.append(item_name)

            # 刷新
            self.log(f"  [商店 #{refresh_num}] 未找到 [{target}]，刷新…")
            self._click_at(refresh_coord["x"], refresh_coord["y"])
            self._sleep_check(delay)

        self.log(f"  ⚠ [商店] 刷新 {max_refreshes} 次未找到 [{target}]，放弃")
        if leave_coord:
            self._click_at(leave_coord["x"], leave_coord["y"])
            self._sleep_check(1.0)
        self._wave_countdown()
        if bought_secondary:
            self.log(f"  🛒 商店购买 (无{target}): {'、'.join(bought_secondary)}")
        return True

    # ---- 单次升级流程 ----

    def trigger_once(self) -> bool:
        """
        循环处理所有箱子 → 最后执行升级。
        返回是否成功执行。
        """
        self.log(f"[{datetime.now().strftime('%H:%M:%S')}] 开始检测…")

        # 确保 _stop_event 是清除的
        stop_was_set = self._stop_event.is_set()
        if stop_was_set:
            self.log(f"  ⚠ _stop_event 在开始时已置位，清除后继续")
            self._stop_event.clear()

        # 1) 循环处理箱子，直到不在箱子界面
        self._current_phase = "crate"
        crate_results: list[dict] = []  # 收集所有箱子处理结果
        while not self._stop_event.is_set():
            # 通用暂停检查
            while self._general_pause.is_set():
                if self._stop_event.is_set():
                    break
                self.status("⏸ 已暂停", "#f39c12")
                time.sleep(0.1)
            if self._stop_event.is_set():
                break
            result = self._handle_crate()
            if result is None:
                break
            crate_results.append(result)
            if len(crate_results) > 999:
                self.log("  ⚠ 箱子处理超过 999 次，强制停止")
                break

        # 2) 单次触发流程：箱子 → 升级 → 商店，任一环节起，至商店"出发"止
        self._current_phase = "idle"

        # 2a) 检测升级
        in_upgrade = self._is_on_upgrade_screen()
        self.log(f"  [界面检测] 升级界面={'✓' if in_upgrade else '✗'}")

        if in_upgrade:
            # 在升级界面：执行升级
            self._current_phase = "upgrade"
            raw_delay = self.config.get("delay_between_upgrades_ms")
            delay = max(100, raw_delay) / 1000.0
            upgrade_max = self.config.data.get("upgrade_max", 0)
            self.log(f"  ✓ 开始升级（间隔 {raw_delay}ms，每击前检测界面，上限 {upgrade_max or '不限'}）")
            loop_limit = upgrade_max if upgrade_max else 99999
            i = 0
            while i < loop_limit:
                if self._stop_event.is_set():
                    self.status("⏹ 已停止", "#e74c3c")
                    self.log(f"  ⏹ 已中止（完成 {i} 次）")
                    return False
                while self._general_pause.is_set():
                    if self._stop_event.is_set():
                        break
                    self.status("⏸ 已暂停", "#f39c12")
                    time.sleep(0.1)
                if self._stop_event.is_set():
                    break
                if not self._is_on_upgrade_screen():
                    self.log(f"  ✓ 已离开升级界面，共完成 {i} 次")
                    break
                self._press_upgrade_once()
                i += 1
                limit_str = f"/{upgrade_max}" if upgrade_max else ""
                self.status(f"⬆ 升级 {i}{limit_str}", "#f1c40f")
                if upgrade_max and i >= upgrade_max:
                    self.log(f"  ✓ 已达上限 {upgrade_max} 次，停止升级")
                    break
                if self._sleep_check(delay):
                    self.status("⏹ 已停止", "#e74c3c")
                    self.log(f"  ⏹ 紧急停止（完成 {i} 次）")
                    return False
                if i % 20 == 0:
                    self.log(f"  …进度: {i} 次")
            if i >= loop_limit and not upgrade_max:
                self.log(f"  ⚠ 升级超过 {loop_limit} 次，强制停止")
            self.status(f"✅ {i} 次升级完成", "#2ecc71")
            self.log(f"  ✅ 完成 {i} 次升级")

        # 2b) 升级结束后 → 商店（延迟 0.5s 等待画面过渡）
        self._sleep_check(1.0)
        self._current_phase = "shop"
        if self._handle_shop(crate_results):
            self.log("  🏁 已出发，单次触发结束")
            self._current_phase = "idle"
            return True

        # 没进商店 → 输出箱子拾取总结后结束
        if crate_results:
            picked = [r for r in crate_results if r["picked"]]
            if picked:
                names = "、".join(r["name"] for r in picked)
                self.log(f"  🎁 拾取 ({len(picked)}个): {names}")
        return True

    # ---- 连续监控模式 ----

    def _post_shop_crate_check(self) -> bool:
        """商店出发后，每秒检测箱子最多 15 次。返回 True 表示找到箱子。"""
        self.log(f"[{datetime.now().strftime('%H:%M:%S')}] 开始 15s 箱子检测")
        for _ in range(15):
            if self._stop_event.is_set():
                return False
            self.status("🔍 检测箱子中…", "#3498db")
            result = self._handle_crate()
            if result is not None:
                return True
            time.sleep(1)
        self.log(f"[{datetime.now().strftime('%H:%M:%S')}] 15s 箱子检测结束，未找到箱子")
        return False

    def _monitor_loop(self):
        """连续模式：与 F7 流程一致，出发后 60 秒倒计时 + 15 秒箱子检测。"""
        self.status("🟢 连续模式运行中", "#2ecc71")
        self.log(f"[{datetime.now().strftime('%H:%M:%S')}] 🟢 连续模式已启动")

        crate_results: list[dict] = []  # 收集本轮箱子拾取结果

        while not self._stop_event.is_set():
            try:
                # ① 箱子（循环处理，最多 10 次防误判死循环）
                self._current_phase = "crate"
                crate_found = False
                crate_loop_count = 0
                while not self._stop_event.is_set() and crate_loop_count < 15:
                    # 通用暂停检查
                    while self._general_pause.is_set():
                        if self._stop_event.is_set():
                            break
                        self.status("⏸ 已暂停", "#f39c12")
                        time.sleep(0.1)
                    if self._stop_event.is_set():
                        break
                    result = self._handle_crate()
                    if result is None:
                        break
                    crate_results.append(result)
                    crate_found = True
                    crate_loop_count += 1
                if crate_found:
                    continue

                # ② 检测升级界面
                self._current_phase = "idle"
                in_upgrade = self._is_on_upgrade_screen()
                self.log(f"  [界面检测] 升级界面={'✓' if in_upgrade else '✗'}")
                if in_upgrade:
                    self._current_phase = "upgrade"
                    raw_delay = self.config.get("delay_between_upgrades_ms")
                    delay = max(100, raw_delay) / 1000.0
                    upgrade_max = self.config.data.get("upgrade_max", 0)
                    self.log(f"[{datetime.now().strftime('%H:%M:%S')}] 开始升级（间隔{raw_delay}ms，上限{upgrade_max or '不限'}）")
                    loop_limit = upgrade_max if upgrade_max else 99999
                    i = 0
                    while i < loop_limit:
                        if self._stop_event.is_set():
                            break
                        while self._general_pause.is_set():
                            if self._stop_event.is_set():
                                break
                            self.status("⏸ 已暂停", "#f39c12")
                            time.sleep(0.1)
                        if self._stop_event.is_set():
                            break
                        if not self._is_on_upgrade_screen():
                            self.log(f"  ✓ 已离开升级界面，共完成 {i} 次")
                            break
                        self._press_upgrade_once()
                        i += 1
                        limit_str = f"/{upgrade_max}" if upgrade_max else ""
                        self.status(f"⬆ 升级 {i}{limit_str}", "#f1c40f")
                        if upgrade_max and i >= upgrade_max:
                            self.log(f"  ✓ 已达上限 {upgrade_max} 次，停止升级")
                            break
                        self._sleep_check(delay)
                    if not self._stop_event.is_set():
                        self.log(f"  ✅ {i} 次升级完成")

                # 紧急停止则跳过商店
                if self._stop_event.is_set():
                    break

                # ④ 商店（延迟 0.5s 等待画面过渡）
                self._sleep_check(1.0)
                self._current_phase = "shop"
                if self._handle_shop(crate_results):
                    # _handle_shop 内部已调用 _wave_countdown
                    crate_results = []  # 新一波开始，清空箱子记录
                    self._post_shop_crate_check()
                    self._shop_cooldown = time.time() + 3  # 箱子检测后再冷却 3 秒
                    continue

                # ⑤ 无事 → 等 1 秒再轮询
                self._current_phase = "idle"
                self.status("🔍 波次检测中…", "#3498db")
                time.sleep(1)
            except Exception as e:
                self.log(f"  ⚠ 监控异常: {e}")
                time.sleep(1)

        self.status("⚪ 待机中", "#cccccc")
        self.log(f"[{datetime.now().strftime('%H:%M:%S')}] 🔴 连续模式已停止")

    # ---- 启停控制 ----

    def start_continuous(self):
        with self._lock:
            if self._running:
                self.log("  ℹ 已在运行中")
                return
            self._running = True
            self._stop_event.clear()
            self._worker_thread = Thread(target=self._monitor_loop, daemon=True)
            self._worker_thread.start()

    def stop_continuous(self):
        with self._lock:
            if not self._running:
                return
            self._stop_event.set()
            self._running = False
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=3)


# ===================================================================
# RegionSelector — 屏幕区域框选工具
# ===================================================================

class RegionSelector(tk.Toplevel):
    """透明全屏覆盖窗口，让用户拖拽框选 OCR 识别区域。"""

    def __init__(self, master, callback=None):
        super().__init__(master)
        self.callback = callback
        self.result_rect: dict | None = None

        # 截取全屏作为背景（monitors[0] = 完整虚拟桌面，含所有显示器）
        self.sct = mss.MSS()
        monitor = self.sct.monitors[0]
        sct_img = self.sct.grab(monitor)
        self.bg_image = Image.fromarray(np.array(sct_img)[:, :, :3][:, :, ::-1])  # BGR→RGB
        self._vw = monitor["width"]
        self._vh = monitor["height"]

        # 窗口设置 — 覆盖整个虚拟桌面
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.geometry(f"{self._vw}x{self._vh}+0+0")

        # 先隐藏，画完再显示
        self.withdraw()

        # Canvas
        self.canvas = tk.Canvas(
            self, highlightthickness=0,
            bg="black", width=self._vw, height=self._vh
        )
        self.canvas.pack(fill=tk.BOTH, expand=True)

        # 将截图转为 PhotoImage
        self._tk_image = self._pil_to_tk(self.bg_image)
        self.canvas.create_image(0, 0, anchor=tk.NW, image=self._tk_image)

        # 半透明遮罩（在截图之上）
        self._overlay = self.canvas.create_rectangle(
            0, 0, self._vw, self._vh, fill="black", stipple="gray50", outline=""
        )

        # 选择矩形
        self._sel_rect = None
        self._start_x = 0
        self._start_y = 0

        # 绑定事件
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Escape>", lambda e: self._cancel())

        # 提示文字
        self._hint = self.canvas.create_text(
            self._vw // 2, self._vh - 40,
            text="拖拽框选右上角等级数字区域  |  按 ESC 取消",
            fill="yellow", font=("Microsoft YaHei", 14, "bold")
        )

        self.deiconify()
        self.focus_force()

    def _pil_to_tk(self, img: Image.Image):
        """PIL → tkinter PhotoImage（保持宽高原样）。"""
        from PIL import ImageTk
        return ImageTk.PhotoImage(img)

    def _on_press(self, event):
        self._start_x = event.x
        self._start_y = event.y
        if self._sel_rect:
            self.canvas.delete(self._sel_rect)
        self._sel_rect = self.canvas.create_rectangle(
            event.x, event.y, event.x, event.y,
            outline="red", width=2, dash=(6, 3)
        )

    def _on_drag(self, event):
        if self._sel_rect:
            self.canvas.coords(self._sel_rect, self._start_x, self._start_y, event.x, event.y)

    def _on_release(self, event):
        x1, y1 = min(self._start_x, event.x), min(self._start_y, event.y)
        x2, y2 = max(self._start_x, event.x), max(self._start_y, event.y)
        width = x2 - x1
        height = y2 - y1

        if width < 10 or height < 10:
            messagebox.showwarning("区域太小", "请框选一个至少 10×10 像素的区域", parent=self)
            return

        self.result_rect = {
            "left": x1,
            "top": y1,
            "width": width,
            "height": height,
        }

        if self.callback:
            self.callback(self.result_rect)

        self.destroy()

    def _cancel(self):
        self.result_rect = None
        if self.callback:
            self.callback(None)
        self.destroy()


# ===================================================================
# MainGUI — 主界面
# ===================================================================
# StatusOverlay — 游戏内实时状态浮窗
# ===================================================================

class StatusOverlay(tk.Toplevel):
    """半透明置顶浮窗，4 行显示，支持拖拽缩放/移动和边缘吸附。"""

    MAX_HISTORY = 3
    BORDER_WIDTH = 12  # 边框拖拽区域宽度（像素）— 不能太窄，区分 resize 和 move
    SNAP_PX = 20       # 吸附距离

    def __init__(self, master, config: ConfigManager, log_callback=None):
        super().__init__(master)
        self.config = config
        self.log = log_callback or (lambda m: None)
        self.overrideredirect(True)
        self.attributes("-topmost", True)

        self._history: list[tuple[str, str]] = []
        self._visible = False
        self._settings_mode = False  # 设置/预览模式（可交互）
        self._preview_active = False

        # 拖拽状态
        self._drag_type = None  # "move" | "resize" | None
        self._drag_start_x = 0
        self._drag_start_y = 0
        self._drag_start_geom = (0, 0, 0, 0)  # x, y, w, h
        self._drag_start_font = 0  # 拖动开始时的字体大小
        self._aspect_ratio = 1.0

        # 子组件引用
        self._line1: tk.Label | None = None
        self._line_sep: tk.Frame | None = None
        self._history_lines: list[tk.Label] = []

        self._apply_config()
        self._build_overlay()

        self.withdraw()
        self.after(100, self._auto_position)

    # ---- 构建与配置 ----

    def _apply_config(self):
        """从 config 读取参数并应用到窗口。"""
        d = self.config.data
        self._x = d.get("overlay_x", 20)
        self._y = d.get("overlay_y", 0)
        self._ov_w = max(200, d.get("overlay_width", 340))
        self._ov_h = max(80, d.get("overlay_height", 120))
        self._aspect_ratio = self._ov_w / self._ov_h
        self._font_size = max(8, d.get("overlay_font_size", 13))
        self._opacity = max(0.1, min(1.0, d.get("overlay_opacity", 0.84)))
        self._snap_margin = max(0, d.get("overlay_snap_margin", self.SNAP_PX))
        self.attributes("-alpha", self._opacity)

    def _write_config(self):
        """将当前几何参数写回 config。"""
        d = self.config.data
        d["overlay_x"] = self._x
        d["overlay_y"] = self._y
        d["overlay_width"] = self._ov_w
        d["overlay_height"] = self._ov_h
        d["overlay_font_size"] = self._font_size
        d["overlay_opacity"] = self._opacity
        self.config.save()

    def _build_overlay(self):
        """重建浮窗内部组件。"""
        for w in self.winfo_children():
            w.destroy()
        self._history_lines = []

        bg = "#1a1a1a"
        self.configure(bg=bg)
        fs = max(8, min(28, int((self._ov_h - 36) / 6)))
        self._font_size = fs
        hfs = fs + 2

        line1_frame = tk.Frame(self, bg=bg)
        line1_frame.pack(fill=tk.X)
        self._line1 = tk.Label(
            line1_frame, text="⚪ 待机中", fg="#cccccc", bg=bg,
            font=("Microsoft YaHei", hfs, "bold"), justify=tk.LEFT,
            padx=8, pady=4, anchor="w"
        )
        self._line1.pack(fill=tk.X)

        self._line_sep = tk.Frame(self, height=1, bg="#444")
        self._line_sep.pack(fill=tk.X, padx=6)

        for _ in range(self.MAX_HISTORY):
            lbl = tk.Label(self, text="", fg="#888888", bg=bg,
                font=("Microsoft YaHei", fs), justify=tk.LEFT, padx=8, anchor="w")
            lbl.pack(fill=tk.X)
            self._history_lines.append(lbl)

        self.geometry(f"{self._ov_w}x{self._ov_h}+{self._x}+{self._y}")

        # 递归绑定鼠标事件到所有子组件（用于光标 + 拖拽）
        def _bind_mouse(w):
            w.bind("<Motion>", self._on_mouse_move, add="+")
            w.bind("<ButtonPress-1>", self._on_mouse_down, add="+")
            w.bind("<B1-Motion>", self._on_mouse_drag, add="+")
            w.bind("<ButtonRelease-1>", self._on_mouse_up, add="+")
            for c in w.winfo_children():
                _bind_mouse(c)
        _bind_mouse(self)

    def _update_appearance(self):
        """仅更新字体和尺寸，字体根据窗口高度自动适配 4 行。"""
        # 字体自动计算：4行（1行状态 + 3行历史）= (_ov_h - 36) / 6
        fs = max(8, min(28, int((self._ov_h - 36) / 6)))
        self._font_size = fs
        hfs = fs + 2
        if self._line1:
            self._line1.config(font=("Microsoft YaHei", hfs, "bold"))
        for lbl in self._history_lines:
            lbl.config(font=("Microsoft YaHei", fs))
        if self._visible or self._settings_mode:
            self.geometry(f"{self._ov_w}x{self._ov_h}+{self._x}+{self._y}")

    # ---- 位置管理 ----

    def _auto_position(self):
        """仅当 y=0 时自动定位到底部（x 保持 config 中的值）。"""
        try:
            if self._y > 0:
                return
            screen_h = self.winfo_screenheight()
            self.update_idletasks()
            h = self.winfo_reqheight() or self._ov_h
            self._y = screen_h - h - 20
            self._ov_h = h
            self.geometry(f"{self._ov_w}x{h}+{self._x}+{self._y}")
        except Exception:
            pass

    def _screen_size(self):
        """获取虚拟桌面尺寸（跨多显示器）。"""
        try:
            return self.winfo_vrootwidth(), self.winfo_vrootheight()
        except Exception:
            return self.winfo_screenwidth(), self.winfo_screenheight()

    def _snap_to_edge(self):
        """边缘吸附检测。"""
        try:
            screen_w, screen_h = self._screen_size()
            m = self._snap_margin
            changed = False
            if self._x < m:
                self._x = 0; changed = True
            elif self._x + self._ov_w > screen_w - m:
                self._x = screen_w - self._ov_w; changed = True
            if self._y < m:
                self._y = 0; changed = True
            elif self._y + self._ov_h > screen_h - m:
                self._y = screen_h - self._ov_h; changed = True
            if changed:
                self.geometry(f"+{self._x}+{self._y}")
        except Exception as e:
            self.log(f"[浮窗吸附] 异常: {e}")

    # ---- 鼠标交互 ----

    def _get_hit_type(self, rel_x: int, rel_y: int):
        """判断相对于 Toplevel 的坐标位置：'resize' | 'move' | None"""
        if not self._settings_mode:
            return None
        bw = self.BORDER_WIDTH
        if (rel_x <= bw or rel_x >= self._ov_w - bw or
            rel_y <= bw or rel_y >= self._ov_h - bw):
            return "resize"
        return "move"

    def _on_mouse_move(self, event):
        """鼠标在浮窗上移动时更新光标（设置模式下）。"""
        if not self._settings_mode:
            return
        rel_x = event.x_root - self.winfo_rootx()
        rel_y = event.y_root - self.winfo_rooty()
        bw = self.BORDER_WIDTH
        on_edge = (rel_x <= bw or rel_x >= self._ov_w - bw or
                   rel_y <= bw or rel_y >= self._ov_h - bw)
        self.configure(cursor="bottom_right_corner" if on_edge else "fleur")

    def _on_mouse_down(self, event):
        rel_x = event.x_root - self.winfo_rootx()
        rel_y = event.y_root - self.winfo_rooty()
        self._drag_type = self._get_hit_type(rel_x, rel_y)
        if not self._drag_type:
            return
        self._drag_start_x = event.x_root
        self._drag_start_y = event.y_root
        self._drag_start_geom = (self._x, self._y, self._ov_w, self._ov_h)
        self._drag_start_font = self._font_size

    def _on_mouse_drag(self, event):
        if not self._drag_type:
            return
        dx = event.x_root - self._drag_start_x
        dy = event.y_root - self._drag_start_y
        sx, sy, sw, sh = self._drag_start_geom

        if self._drag_type == "move":
            new_x = sx + dx
            new_y = sy + dy
            self._x = new_x
            self._y = new_y
            self.geometry(f"+{new_x}+{new_y}")
        elif self._drag_type == "resize":
            # 按比例缩放：基于水平方向的变化
            new_w = max(200, sw + dx)
            new_h = max(80, int(new_w / self._aspect_ratio))
            # 字体始终基于拖动开始时的基准字体缩放
            scale = new_w / max(sw, 1)
            base_fs = self._drag_start_font
            new_fs = max(8, int(base_fs * scale))
            self._font_size = new_fs
            self._ov_w = new_w
            self._ov_h = new_h
            self._update_appearance()
            self._snap_to_edge()

    def _on_mouse_up(self, event):
        if self._drag_type == "move":
            self._snap_to_edge()
        if self._drag_type:
            self._write_config()
            self._drag_type = None

    def close_preview(self):
        """关闭预览：隐藏浮窗 + 重置状态 + 保存配置。"""
        self._settings_mode = False
        self._preview_active = False
        self._visible = False
        try:
            self.configure(highlightthickness=0)
        except Exception as e:
            self.log(f"[浮窗ERROR] highlightthickness: {e}")
        try:
            self.configure(cursor="arrow")
        except Exception as e:
            self.log(f"[浮窗ERROR] cursor: {e}")
        try:
            self.geometry(f"+{-9999}+{-9999}")
        except Exception as e:
            self.log(f"[浮窗ERROR] geometry: {e}")
        try:
            self.attributes("-alpha", 0.0)
        except Exception as e:
            self.log(f"[浮窗ERROR] alpha: {e}")
        # 清除预览模式下的警告行
        if self._history_lines:
            try:
                self._history_lines[0].config(text="", fg="#888888")
            except Exception:
                pass
        self._write_config()

    # ---- 公共接口 ----

    def set_settings_mode(self, active: bool):
        """开启设置模式（active=True）供拖拽调整。"""
        self._settings_mode = active
        if active:
            self._preview_active = True
            try:
                self.geometry(f"{self._ov_w}x{self._ov_h}+{self._x}+{self._y}")
                self.attributes("-alpha", self._opacity)
                self.configure(highlightbackground="#4ecdc4", highlightthickness=2)
                self.deiconify()
                self._visible = True
            except Exception as e:
                self.log(f"[浮窗ERROR] set_settings_mode(True): {e}")
        else:
            self._settings_mode = False
            self._preview_active = False
            try:
                self.configure(cursor="arrow")
                self.configure(highlightthickness=0)
            except Exception as e:
                self.log(f"[浮窗ERROR] set_settings_mode(False): {e}")

    def set_preview_text(self, text: str):
        """设置预览文本（第 1 行 + 第 2 行警告）。"""
        if self._line1:
            self._line1.config(text=text, fg="#4ecdc4")
        if self._history_lines:
            self._history_lines[0].config(
                text="  ⚠ 注意不要放在会遮挡商店点击的位置",
                fg="#e67e22")

    @property
    def is_preview_active(self) -> bool:
        return self._preview_active

    def set_status(self, text: str, color: str = "#cccccc"):
        """更新当前操作行（第 1 行）。"""
        if self._preview_active:
            return
        if not self._visible:
            try:
                self._auto_position()
                self.geometry(f"{self._ov_w}x{self._ov_h}+{self._x}+{self._y}")
                self.attributes("-alpha", self._opacity)
                self.deiconify()
                self._visible = True
            except Exception:
                pass
        if self._line1:
            try:
                self._line1.config(text=text, fg=color)
            except Exception:
                pass

    def add_history(self, text: str, color: str = "#aaaaaa"):
        """追加一条历史记录（第 2-4 行滚动显示）。"""
        if self._preview_active:
            return
        try:
            self._history.append((text, color))
            if len(self._history) > self.MAX_HISTORY:
                self._history = self._history[-self.MAX_HISTORY:]
            for i, lbl in enumerate(self._history_lines):
                if i < len(self._history):
                    t, c = self._history[-(i + 1)]
                    lbl.config(text=f"  {t}", fg=c)
                else:
                    lbl.config(text="")
        except Exception:
            pass

    def hide(self):
        try:
            self._preview_active = False
            self._visible = False
            self.geometry(f"+{-9999}+{-9999}")
            self.attributes("-alpha", 0.0)
        except Exception:
            pass


# ===================================================================
# ShortcutConfigWindow — 快捷键设置弹窗
# ===================================================================

class ShortcutConfigWindow(tk.Toplevel):
    """快捷键配置窗口，字体跟随主界面。"""

    ACTIONS = [
        ("stop",        "紧急停止"),
        ("toggle",      "切换连续模式"),
        ("trigger_once","单次触发"),
        ("test_crate",  "测试箱子检测"),
        ("test_item",   "测试道具识别"),
        ("pause",              "暂停/恢复"),
        ("test_upgrade_detect", "测试升级检测"),
    ]

    def __init__(self, master, config, font_default, font_bold):
        super().__init__(master)
        self.title("快捷键设置")
        self.resizable(False, False)
        self.configure(padx=12, pady=8)
        self.config = config
        self._font = font_default
        self._font_bold = font_bold
        self._listening_key: str | None = None  # 正在等待按键的 action

        # 启用开关
        self._enabled_var = tk.BooleanVar(value=config.data.get("shortcuts_enabled", True))
        ttk.Checkbutton(self, text="启用全局快捷键", variable=self._enabled_var).pack(anchor=tk.W, pady=(0, 8))

        # 表头
        hdr = ttk.Frame(self)
        hdr.pack(fill=tk.X)
        ttk.Label(hdr, text="功能", font=self._font_bold, width=16, anchor=tk.W).pack(side=tk.LEFT)
        ttk.Label(hdr, text="快捷键", font=self._font_bold, width=10).pack(side=tk.LEFT)

        sep = ttk.Separator(self, orient=tk.HORIZONTAL)
        sep.pack(fill=tk.X, pady=2)

        # 各行
        self._rows: dict[str, dict] = {}  # action -> {"label": tk.Label, "btn": ttk.Button}
        shortcuts = config.data.get("shortcuts", {})
        for action, label in self.ACTIONS:
            row = ttk.Frame(self)
            row.pack(fill=tk.X, pady=1)
            ttk.Label(row, text=label, font=self._font, width=16, anchor=tk.W).pack(side=tk.LEFT)
            key_display = shortcuts.get(action, "无")
            btn = ttk.Button(row, text=key_display, width=10,
                             command=lambda a=action: self._start_listen(a))
            btn.pack(side=tk.LEFT, padx=(4, 0))
            self._rows[action] = {"label_text": label, "btn": btn, "key": shortcuts.get(action, "")}

        # 底部按钮
        btn_row = ttk.Frame(self)
        btn_row.pack(fill=tk.X, pady=(12, 0))
        ttk.Button(btn_row, text="清除所有快捷键", command=self._clear_all).pack(side=tk.LEFT)
        ttk.Button(btn_row, text="💾 保存", command=self._save).pack(side=tk.RIGHT)
        ttk.Button(btn_row, text="取消", command=self.destroy).pack(side=tk.RIGHT, padx=4)

        self.bind("<KeyPress>", self._on_key_press)
        self.focus_set()

    def _start_listen(self, action: str):
        """开始监听按键。"""
        self._listening_key = action
        for a, r in self._rows.items():
            if a == action:
                r["btn"].config(text="... 按键 ...")
        self._append_log(f"[快捷键] 等待按键: {dict(self.ACTIONS).get(action, action)}")

    def _on_key_press(self, event):
        """捕获按键。"""
        if self._listening_key is None:
            return
        action = self._listening_key
        self._listening_key = None

        # 转成 pynput 格式（tkinter keysym → pynput Key.name）
        _TK_TO_PYNPUT = {"escape": "esc", "return": "enter", "backspace": "backspace"}
        keysym_lower = event.keysym.lower()
        pynput_name = _TK_TO_PYNPUT.get(keysym_lower, keysym_lower)
        if keysym_lower in ("escape", "f1", "f2", "f3", "f4", "f5", "f6", "f7", "f8",
                            "f9", "f10", "f11", "f12", "space", "return", "tab",
                            "backspace", "delete", "up", "down", "left", "right"):
            canon = f"<{pynput_name}>"
        elif len(event.char) == 1 and event.char.isprintable() and event.char != ' ':
            canon = event.char.lower()
        else:
            canon = f"<{pynput_name}>"

        # 冲突检查
        if canon:
            conflict_action = None
            for a, r in self._rows.items():
                if a != action and r["key"] == canon:
                    conflict_action = a
                    break
            if conflict_action:
                conflict_label = dict(self.ACTIONS).get(conflict_action, conflict_action)
                self_label = dict(self.ACTIONS).get(action, action)
                ok = messagebox.askyesno(
                    "快捷键冲突",
                    f"「{self_label}」设置的 [{canon}] 已被「{conflict_label}」占用。\n\n"
                    f"确认覆盖？\n（「{conflict_label}」的快捷键将被清除）",
                    parent=self
                )
                if not ok:
                    # 取消：恢复旧值
                    old_key = self._rows[action].get("key", "")
                    display = old_key if old_key else "无"
                    self._rows[action]["btn"].config(text=display)
                    return
                # 覆盖：清除冲突项
                self._rows[conflict_action]["key"] = ""
                self._rows[conflict_action]["btn"].config(text="无")

        self._rows[action]["key"] = canon
        self._rows[action]["btn"].config(text=canon)

    def _clear_all(self):
        for r in self._rows.values():
            r["key"] = ""
            r["btn"].config(text="无")
        self._listening_key = None

    def _save(self):
        shortcuts = {a: r["key"] for a, r in self._rows.items()}
        self.config.data["shortcuts"] = shortcuts
        self.config.data["shortcuts_enabled"] = self._enabled_var.get()
        self.config.save()
        self.destroy()

    def _append_log(self, msg):
        pass  # 由外部设置


# ===================================================================

class MainApp(tk.Tk):
    """主 GUI：配置、预览、状态显示。"""

    def __init__(self):
        super().__init__()
        self.title("土豆兄弟·一键托管工具")
        self.geometry("1300x900")
        self.resizable(True, True)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # 配置 & 核心
        self.config = ConfigManager()

        # 字体 — 从配置读取大小
        fs = self.config.get("font_size")
        if fs < 10:
            fs = 16
        self.font_default = ("Microsoft YaHei", fs)
        self.font_bold = ("Microsoft YaHei", fs, "bold")
        self.font_title = ("Microsoft YaHei", fs + 3, "bold")
        self.font_log = ("Consolas", max(8, fs - 2))
        self.font_tip = ("Microsoft YaHei", max(8, fs - 3))

        # 跟踪所有使用字体的 widget，以便动态调整大小
        self._font_tags: list[tuple[tk.Widget, str]] = []  # (widget, role)

        # 依赖检查
        if not HAS_CV2:
            self._show_dependency_warning()

        # 游戏内状态浮窗
        self.overlay = StatusOverlay(self, self.config, log_callback=self._append_log)

        # 核心
        self.upgrader = AutoUpgrader(
            self.config,
            log_callback=self._append_log,
            status_callback=self._update_overlay,
            history_callback=self._overlay_history,
            history_clear_callback=self._clear_overlay_history
        )

        # 热键监听
        self._shortcuts_suspended = False  # 快捷键设置窗口打开时暂停
        self._hotkey_listener: keyboard.Listener | None = None
        self._start_hotkey_listener()

        # 构建 UI
        self._build_ui()

        # 应用字体大小（包括 ttk 样式）
        self._apply_font_size(fs)

        # 刷新一次状态
        self._update_status()

    # ---- 依赖警告 ----

    def _show_dependency_warning(self):
        missing = []
        if not HAS_CV2:
            missing.append("opencv-python")
        msg = (
            "以下依赖未安装，请先运行：\n\n"
            f"  pip install {' '.join(missing)}"
        )
        messagebox.showwarning("缺少依赖", msg)

    # ---- 快捷键辅助 ----

    def _get_shortcut_display(self, action: str) -> str:
        """从配置读取指定 action 的快捷键并格式化为友好文本。"""
        sc = self.config.data.get("shortcuts", {}).get(action, "")
        if not sc:
            return "未设置"
        return self._key_display(sc)

    def _key_display(self, key: str) -> str:
        """将 pynput 格式按键 (<f8>/t/<space>) 转为友好显示 (F8/T/SPACE)。"""
        if not key:
            return "无"
        if key.startswith("<") and key.endswith(">"):
            return key[1:-1].upper()
        return key.upper()

    # ---- 热键 ----

    def _start_hotkey_listener(self):
        """启动全局热键监听（快捷键从 config 动态读取）。"""
        def _for_canonical(key) -> str:
            try:
                return key.char
            except AttributeError:
                return f"<{key.name}>"

        # action → callback 映射
        action_map = {
            "stop":         self._emergency_stop,
            "toggle":       self._on_toggle,
            "trigger_once": self._on_trigger_once,
            "test_crate":   self._test_crate_detect,
            "test_item":    self._test_item_ocr,
            "pause":              self._on_pause,
            "test_upgrade_detect": self._test_upgrade_detect,
        }

        current = set()

        def _safe_after(callback):
            """安排回调到主线程。"""
            self.after(0, callback)

        def _get_shortcut(action: str) -> str | None:
            """读取当前快捷键配置，返回 canonical key 或 None。"""
            if not self.config.data.get("shortcuts_enabled", True):
                return None
            return self.config.data.get("shortcuts", {}).get(action)

        def _find_action(canon: str) -> str | None:
            """根据按键找到对应的 action。"""
            for action in action_map:
                sc = _get_shortcut(action)
                if sc and sc == canon:
                    return action
            return None

        def on_press(key):
            try:
                canon = _for_canonical(key)
                current.add(canon)
            except Exception:
                pass

        def on_release(key):
            try:
                canon = _for_canonical(key)
            except Exception:
                return
            # 紧急停止始终生效（不受快捷键配置窗口影响）
            action = _find_action(canon)
            if action == "stop":
                _safe_after(action_map[action])
                current.discard(canon)
                return
            # 快捷键设置窗口打开期间暂停其他快捷键
            if self._shortcuts_suspended:
                return
            if action is None:
                current.discard(canon)
                return
            if action in ("toggle", "trigger_once"):
                if canon in current:
                    _safe_after(action_map[action])
            else:
                _safe_after(action_map[action])
            current.discard(canon)

        self._hotkey_listener = keyboard.Listener(
            on_press=on_press,
            on_release=on_release,
        )
        self._hotkey_listener.daemon = True
        self._hotkey_listener.start()

    def _on_toggle(self):
        """热键：切换连续模式。"""
        # 预览模式开启则自动关闭
        if self.overlay.is_preview_active:
            self._on_overlay_exit_settings()
        if self.upgrader.is_running:
            self.upgrader.stop_continuous()
        else:
            self.upgrader.start_continuous()
        self.after(0, self._update_status)

    def _on_trigger_once(self):
        """热键：执行一次性升级。"""
        if self.overlay.is_preview_active:
            self._on_overlay_exit_settings()
        if self.upgrader.is_running:
            self._append_log("  ℹ 连续模式已开启，单次触发被忽略。请先关闭连续模式。")
            return
        # 在后台线程执行，避免阻塞热键回调
        def _run():
            self.upgrader.trigger_once()
            self.after(0, self._update_status)
        Thread(target=_run, daemon=True).start()

    def _on_pause(self):
        """F6 暂停：所有环节统一切换暂停/恢复。"""
        if self.upgrader._general_pause.is_set():
            self.upgrader._general_pause.clear()
            self._append_log("[暂停] 恢复运行")
            self.overlay.set_status("⚪ 待机中", "#cccccc")
        else:
            self.upgrader._general_pause.set()
            pause_key = self._get_shortcut_display("pause")
            self._append_log(f"[暂停] 已暂停（再按 {pause_key} 恢复）")
            self.overlay.set_status("⏸ 已暂停", "#f39c12")

    def _emergency_stop(self):
        """紧急强制停止所有操作（包括 F7 单次触发和 F8 连续模式）。"""
        self._append_log(f"[{datetime.now().strftime('%H:%M:%S')}] 🛑 紧急停止！")
        self.upgrader._stop_event.set()
        self.upgrader._running = False
        self.upgrader._general_pause.clear()  # 解除暂停阻塞
        # 清空浮窗历史
        self._clear_overlay_history()
        self.overlay.set_status("⚪ 待机中", "#cccccc")
        self.after(0, self._update_status)

    # ---- 日志 ----

    def _append_log(self, msg: str):
        """向日志区域追加一行。"""
        self.log_text.configure(state="normal")
        self.log_text.insert(tk.END, msg + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state="disabled")

    def _update_overlay(self, text: str, color: str = "#cccccc"):
        """更新游戏内状态浮窗（当前操作行）。"""
        self.overlay.set_status(text, color)

    def _overlay_history(self, text: str, color: str = "#aaaaaa"):
        """追加历史记录到浮窗。"""
        self.overlay.add_history(text, color)

    def _clear_overlay_history(self):
        """清空浮窗历史记录。"""
        self.overlay._history = []
        for lbl in self.overlay._history_lines:
            lbl.config(text="")

    # ---- 浮窗设置 ----

    def _overlay_pos_text(self) -> str:
        """生成浮窗位置文本。"""
        d = self.config.data
        return f"X:{d.get('overlay_x',20)} Y:{d.get('overlay_y',0)} {d.get('overlay_width',340)}×{d.get('overlay_height',120)}"

    def _refresh_overlay_pos_label(self):
        """刷新位置标签和opacity标签。"""
        self.lbl_overlay_pos.config(text=self._overlay_pos_text())

    def _on_overlay_enable_toggle(self):
        """浮窗启用开关。"""
        enabled = self.overlay_enabled_var.get()
        self.config.data["overlay_enabled"] = enabled
        if self.overlay.is_preview_active:
            self.overlay.close_preview()
            self.btn_preview.config(text="🔍 预览")
        if enabled:
            self.overlay._auto_position()
            self.overlay.geometry(f"{self.overlay._ov_w}x{self.overlay._ov_h}+{self.overlay._x}+{self.overlay._y}")
            self.overlay.attributes("-alpha", self.overlay._opacity)
            self.overlay.deiconify()
            self.overlay._visible = True
        else:
            self.overlay.hide()
        self.config.save()

    def _on_overlay_preview(self):
        """预览浮窗（面板按钮）。"""
        if self.overlay.is_preview_active:
            try:
                self.overlay.close_preview()
            except Exception as e:
                self._append_log(f"[浮窗ERROR] close_preview: {e}")
            self.btn_preview.config(text="🔍 预览")
            self._refresh_overlay_pos_label()
        else:
            self.overlay.set_settings_mode(True)
            preview_text = self.entry_preview.get().strip() or "🎮 自动升级运行中…"
            self.overlay.set_preview_text(preview_text)
            self.btn_preview.config(text="⏹ 关闭预览")

    def _on_overlay_opacity(self, val):
        """不透明度滑块。"""
        opacity = float(val) / 100.0
        self.overlay.attributes("-alpha", opacity)
        self.config.data["overlay_opacity"] = opacity

    def _on_overlay_reset(self):
        """重置浮窗到底部（x 保持当前值）。"""
        try:
            screen_h = self.overlay.winfo_screenheight()
            h = self.overlay._ov_h
            new_y = screen_h - h - 20
            self.config.data["overlay_y"] = new_y
            self.overlay._y = new_y
            self.overlay.geometry(f"+{self.overlay._x}+{new_y}")
            self.config.save()
            self._refresh_overlay_pos_label()
            self._append_log(f"[浮窗] 已重置 Y 到底部 ({new_y})")
        except Exception as e:
            self._append_log(f"[浮窗] 重置失败: {e}")

    def _on_overlay_exit_settings(self):
        """退出预览（启动程序时自动调用）。"""
        if self.overlay.is_preview_active:
            self.overlay.close_preview()
            self.btn_preview.config(text="🔍 预览")
            if self.config.data.get("overlay_enabled", True):
                self.overlay._auto_position()
                self.overlay.geometry(f"{self.overlay._ov_w}x{self.overlay._ov_h}+{self.overlay._x}+{self.overlay._y}")
                self.overlay.attributes("-alpha", self.overlay._opacity)
                self.overlay.deiconify()
                self.overlay._visible = True

    # ---- 状态刷新 ----

    def _update_status(self):
        if self.upgrader.is_running:
            self.lbl_status.config(text="🟢 连续模式运行中", foreground="green")
            self.btn_toggle.config(text="停止连续模式")
        else:
            self.lbl_status.config(text="⚪ 待机中", foreground="gray")
            self.btn_toggle.config(text="启动连续模式")

    # ---- 字体管理 ----

    def _tag(self, widget: tk.Widget, role: str) -> tk.Widget:
        """标记 widget 的字体角色，返回 widget 以便链式调用。"""
        self._font_tags.append((widget, role))
        return widget

    def _apply_font_size(self, fs: int):
        """动态更新所有标记 widget 的字体大小，以及 ttk 全局样式。"""
        self.font_default = ("Microsoft YaHei", fs)
        self.font_bold = ("Microsoft YaHei", fs, "bold")
        self.font_title = ("Microsoft YaHei", fs + 3, "bold")
        self.font_log = ("Consolas", max(8, fs - 2))
        self.font_tip = ("Microsoft YaHei", max(8, fs - 3))

        font_map = {
            "default": self.font_default,
            "bold": self.font_bold,
            "title": self.font_title,
            "log": self.font_log,
            "tip": self.font_tip,
        }
        for widget, role in self._font_tags:
            if not widget.winfo_exists():
                continue
            try:
                widget.config(font=font_map.get(role, self.font_default))
            except tk.TclError:
                pass

        # 更新 ttk 全局样式：小标题、按钮、复选框
        style = ttk.Style()
        style.configure("TLabelframe.Label", font=self.font_bold)
        style.configure("TButton", font=self.font_default, padding=max(2, fs//4))
        style.configure("TCheckbutton", font=self.font_default)

    # ---- UI 构建 ----

    def _build_ui(self):
        main_frame = ttk.Frame(self, padding=8)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # Pack 布局: 标题 → 内容(扩展) → 分隔线 → 日志 → 提示
        # ====== 标题 ======
        title_f = ttk.Frame(main_frame)
        title_f.pack(fill=tk.X)
        self._tag(ttk.Label(title_f, text="🎮 土豆兄弟·一键托管工具",
                            font=self.font_title), "title").pack(anchor=tk.W)
        self._tag(ttk.Label(title_f, text="自动升级 → 拾取箱子 → 商店购买",
                            font=self.font_default), "default").pack(anchor=tk.W, pady=(2, 4))

        # ====== 左右分栏（扩展填充剩余空间） ======
        panes = ttk.Frame(main_frame)
        panes.pack(fill=tk.BOTH, expand=True)
        panes.grid_columnconfigure(0, weight=1)
        panes.grid_columnconfigure(1, weight=1)
        panes.grid_rowconfigure(0, weight=1)

        def _make_scrollable(parent, col):
            """创建带水平和垂直滚动条的 Canvas + Frame。返回 (canvas, inner_frame)。"""
            outer = ttk.Frame(parent)
            outer.grid(row=0, column=col, sticky="nsew")
            outer.grid_rowconfigure(0, weight=1)
            outer.grid_columnconfigure(0, weight=1)

            canvas = tk.Canvas(outer, highlightthickness=0)
            h_scroll = ttk.Scrollbar(outer, orient=tk.HORIZONTAL, command=canvas.xview)
            v_scroll = ttk.Scrollbar(outer, orient=tk.VERTICAL, command=canvas.yview)
            inner = ttk.Frame(canvas)

            inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
            canvas.create_window((0, 0), window=inner, anchor=tk.NW)
            canvas.configure(xscrollcommand=h_scroll.set, yscrollcommand=v_scroll.set)

            canvas.grid(row=0, column=0, sticky="nsew")
            v_scroll.grid(row=0, column=1, sticky="ns")
            h_scroll.grid(row=1, column=0, sticky="ew")

            def _on_wheel(event):
                # 仅当内容超出可视区域时才滚动
                bbox = canvas.bbox("all")
                if bbox and bbox[3] > canvas.winfo_height():
                    canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
            canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", _on_wheel))
            canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))

            # Shift+滚轮 = 水平滚动
            def _on_shift_wheel(event):
                canvas.xview_scroll(int(-1 * (event.delta / 120)), "units")
            canvas.bind("<Shift-MouseWheel>", _on_shift_wheel)

            return canvas, inner

        left_canvas, left = _make_scrollable(panes, 0)
        right_canvas, right = _make_scrollable(panes, 1)

        # 第一行：保存按钮
        ttk.Button(left, text="💾 保存所有配置", command=self._save_config).pack(anchor=tk.W, pady=(0, 6))



        # 升级与操作设置
        key_frame = ttk.LabelFrame(left, text="⚙ 升级与操作", padding=6)
        key_frame.pack(fill=tk.X, pady=(0, 4))
        rk = ttk.Frame(key_frame); rk.pack(fill=tk.X)
        self._tag(ttk.Label(rk, text="间隔:", font=self.font_default), "default").pack(side=tk.LEFT)
        self.entry_delay = ttk.Entry(rk, width=5); self.entry_delay.pack(side=tk.LEFT, padx=2)
        self.entry_delay.insert(0, str(self.config.get("delay_between_upgrades_ms")))
        ttk.Label(rk, text="ms", font=self.font_default).pack(side=tk.LEFT)
        self._tag(ttk.Label(rk, text="  上限:", font=self.font_default), "default").pack(side=tk.LEFT, padx=(12,0))
        self.entry_upgrade_max = ttk.Entry(rk, width=4); self.entry_upgrade_max.pack(side=tk.LEFT, padx=2)
        self.entry_upgrade_max.insert(0, str(self.config.get("upgrade_max")))
        ttk.Label(rk, text="次(0=不限)", font=self.font_tip).pack(side=tk.LEFT)
        rk3 = ttk.Frame(key_frame); rk3.pack(fill=tk.X, pady=(2,0))
        self._tag(ttk.Label(rk3, text="升级点击:", font=self.font_default), "default").pack(side=tk.LEFT)
        self._make_coord_row(rk3, "upgrade_click", "upgrade_click", delay=True)
        ttk.Label(rk3, text="(必须设置)", font=self.font_tip).pack(side=tk.LEFT, padx=4)
        self._tag(ttk.Label(rk, text="  字体:", font=self.font_default), "default").pack(side=tk.LEFT, padx=(8,0))
        self.spin_font = ttk.Spinbox(rk, from_=10, to=32, width=3, command=self._on_font_size_change)
        self.spin_font.pack(side=tk.LEFT, padx=2)
        self.spin_font.set(str(self.config.get("font_size")))
        # 升级界面检测区域
        rk_upg = ttk.Frame(key_frame); rk_upg.pack(fill=tk.X, pady=(2, 0))
        ttk.Label(rk_upg, text="升级检测:", font=self.font_default).pack(side=tk.LEFT)
        self._make_region_row(rk_upg, "upgrade_detect", "upgrade_detect", delay=True)

        # 拾取箱子设置
        crate_frame = ttk.LabelFrame(left, text="🎁 拾取箱子", padding=6)
        crate_frame.pack(fill=tk.X, pady=(0, 4))
        cr_enable = ttk.Frame(crate_frame); cr_enable.pack(fill=tk.X)
        self.crate_enabled_var = tk.BooleanVar(value=self.config.data.get("crate_enabled", True))
        ttk.Checkbutton(cr_enable, text="启用拾取箱子", variable=self.crate_enabled_var).pack(anchor=tk.W)
        self.crate_auto_skip_var = tk.BooleanVar(value=self.config.data.get("crate_auto_skip", False))
        ttk.Checkbutton(cr_enable, text="  自动跳过所有物品", variable=self.crate_auto_skip_var,
                        command=lambda: self.config.data.update({"crate_auto_skip": self.crate_auto_skip_var.get()})).pack(anchor=tk.W)
        cr0 = ttk.Frame(crate_frame); cr0.pack(fill=tk.X)
        self._tag(ttk.Label(cr0, text="期望道具:", font=self.font_default), "default").pack(side=tk.LEFT)
        self.entry_desired_items = ttk.Entry(cr0, width=24); self.entry_desired_items.pack(side=tk.LEFT, padx=2)
        self.entry_desired_items.insert(0, ", ".join(self.config.data.get("desired_items", [])))
        self._tag(ttk.Label(cr0, text=" 延迟:", font=self.font_default), "default").pack(side=tk.LEFT, padx=(4,0))
        self.entry_crate_delay = ttk.Entry(cr0, width=5); self.entry_crate_delay.pack(side=tk.LEFT, padx=2)
        self.entry_crate_delay.insert(0, str(self.config.data.get("crate_delay_ms", 800)))
        ttk.Label(cr0, text="ms", font=self.font_default).pack(side=tk.LEFT)
        # 箱子检测区域
        cr1 = ttk.Frame(crate_frame); cr1.pack(fill=tk.X, pady=(4,0))
        ttk.Label(cr1, text="\"属性\"区:", font=self.font_default).pack(side=tk.LEFT)
        self._make_region_row(cr1, "crate_detect_region_", "crate", delay=True)
        cr2 = ttk.Frame(crate_frame); cr2.pack(fill=tk.X, pady=(2,0))
        ttk.Label(cr2, text="道具名区:", font=self.font_default).pack(side=tk.LEFT)
        self._make_region_row(cr2, "item_detect_region_", "item", delay=True)
        self._crate_region_to_entries()

        # 商店购买设置
        shop_frame = ttk.LabelFrame(left, text="🛒 商店购买", padding=6)
        shop_frame.pack(fill=tk.X)
        sr_enable = ttk.Frame(shop_frame); sr_enable.pack(fill=tk.X)
        self.shop_enabled_var = tk.BooleanVar(value=self.config.data.get("shop_enabled", True))
        ttk.Checkbutton(sr_enable, text="启用商店购买", variable=self.shop_enabled_var).pack(anchor=tk.W)
        self.shop_auto_skip_var = tk.BooleanVar(value=self.config.data.get("shop_auto_skip", False))
        ttk.Checkbutton(sr_enable, text="  自动跳过（直接出发）", variable=self.shop_auto_skip_var,
                        command=lambda: self.config.data.update({"shop_auto_skip": self.shop_auto_skip_var.get()})).pack(anchor=tk.W)
        sr0 = ttk.Frame(shop_frame); sr0.pack(fill=tk.X)
        ttk.Label(sr0, text="目标:", font=self.font_default).pack(side=tk.LEFT, padx=(4,0))
        self.entry_shop_target = ttk.Entry(sr0, width=8); self.entry_shop_target.pack(side=tk.LEFT, padx=2)
        self.entry_shop_target.insert(0, self.config.data.get("shop_target_item", "沙漏"))
        ttk.Label(sr0, text="次级:", font=self.font_default).pack(side=tk.LEFT, padx=(4,0))
        self.entry_shop_secondary = ttk.Entry(sr0, width=14); self.entry_shop_secondary.pack(side=tk.LEFT, padx=2)
        self.entry_shop_secondary.insert(0, ", ".join(self.config.data.get("shop_secondary_items", [])))
        ttk.Label(sr0, text="最大刷新:", font=self.font_default).pack(side=tk.LEFT, padx=(4,0))
        self.entry_shop_refreshes = ttk.Entry(sr0, width=5); self.entry_shop_refreshes.pack(side=tk.LEFT, padx=2)
        self.entry_shop_refreshes.insert(0, str(self.config.data.get("shop_max_refreshes", 2000)))
        ttk.Label(sr0, text="延迟:", font=self.font_default).pack(side=tk.LEFT, padx=(2,0))
        self.entry_shop_delay = ttk.Entry(sr0, width=4); self.entry_shop_delay.pack(side=tk.LEFT, padx=2)
        self.entry_shop_delay.insert(0, str(self.config.data.get("shop_delay_ms", 500)))
        # 商店检测区域
        sr_d = ttk.Frame(shop_frame); sr_d.pack(fill=tk.X, pady=(4,0))
        ttk.Label(sr_d, text="商店检测:", font=self.font_default).pack(side=tk.LEFT)
        self._make_region_row(sr_d, "shop_detect", "shop_detect_region")
        # 槽位
        for i in range(4):
            sr_s = ttk.Frame(shop_frame); sr_s.pack(fill=tk.X, pady=1)
            ttk.Label(sr_s, text=f"槽{i+1}:", font=self.font_default, width=4).pack(side=tk.LEFT)
            self._make_region_row(sr_s, f"shop_slot_{i}", f"shop_slot_{i}")
            ttk.Label(sr_s, text="买:", font=self.font_default).pack(side=tk.LEFT)
            self._make_coord_row(sr_s, f"shop_buy_{i}", f"shop_buy_{i}")
        # 刷新/出发
        sr_b = ttk.Frame(shop_frame); sr_b.pack(fill=tk.X, pady=(4,0))
        ttk.Label(sr_b, text="刷新:", font=self.font_default).pack(side=tk.LEFT)
        self._make_coord_row(sr_b, "shop_refresh", "shop_refresh")
        ttk.Label(sr_b, text="  出发:", font=self.font_default).pack(side=tk.LEFT, padx=(12,0))
        self._make_coord_row(sr_b, "shop_leave", "shop_leave")
        self._load_shop_entries()

        # --- 右侧：测试面板（填入可滚动 Canvas） ---
        # 操作 & 状态
        ctrl_frame = ttk.LabelFrame(right, text="操作控制", padding=6)
        ctrl_frame.pack(fill=tk.X, pady=(0, 4))
        cr = ttk.Frame(ctrl_frame); cr.pack(fill=tk.X)
        self.btn_toggle = ttk.Button(cr, text="▶ 连续模式", command=self._toggle)
        self.btn_toggle.pack(side=tk.LEFT, padx=(0, 4))
        self.btn_trigger = ttk.Button(cr, text="▶ 单次触发", command=self._trigger_once)
        self.btn_trigger.pack(side=tk.LEFT)
        cr2 = ttk.Frame(ctrl_frame); cr2.pack(fill=tk.X, pady=(4,0))
        self.lbl_status = self._tag(ttk.Label(cr2, text="⚪ 待机中", font=self.font_bold, foreground="gray"), "bold")
        self.lbl_status.pack(side=tk.LEFT)
        stop_key = self._get_shortcut_display("stop")
        self.btn_stop = self._tag(tk.Button(
            cr2, text=f"🛑 紧急停止", command=self._emergency_stop,
            bg="#e74c3c", fg="white", font=self.font_bold,
            activebackground="#c0392b", activeforeground="white",
            relief=tk.RAISED, bd=3, padx=6, pady=1, cursor="hand2"
        ), "bold")
        self.btn_stop.pack(side=tk.RIGHT)
        ttk.Button(cr2, text="⌨ 快捷键", command=self._open_shortcut_config).pack(side=tk.RIGHT, padx=4)

        # 浮窗设置
        overlay_frame = ttk.LabelFrame(right, text="📟 浮窗设置", padding=6)
        overlay_frame.pack(fill=tk.X, pady=(0, 4))

        # 启用
        of0 = ttk.Frame(overlay_frame); of0.pack(fill=tk.X)
        self.overlay_enabled_var = tk.BooleanVar(value=self.config.data.get("overlay_enabled", True))
        ttk.Checkbutton(of0, text="启用游戏内浮窗", variable=self.overlay_enabled_var,
                        command=self._on_overlay_enable_toggle).pack(anchor=tk.W)
        # 预览
        of1 = ttk.Frame(overlay_frame); of1.pack(fill=tk.X, pady=(2, 0))
        self._tag(ttk.Label(of1, text="预览文本:", font=self.font_default), "default").pack(side=tk.LEFT)
        self.entry_preview = ttk.Entry(of1, width=18)
        self.entry_preview.pack(side=tk.LEFT, padx=2)
        self.entry_preview.insert(0, "🎮 自动升级运行中…")
        self.btn_preview = ttk.Button(of1, text="🔍 预览", command=self._on_overlay_preview, width=6)
        self.btn_preview.pack(side=tk.LEFT, padx=2)
        # 外观
        of2 = ttk.Frame(overlay_frame); of2.pack(fill=tk.X, pady=(2, 0))
        self._tag(ttk.Label(of2, text="不透明度:", font=self.font_default), "default").pack(side=tk.LEFT)
        self.scale_opacity = ttk.Scale(of2, from_=10, to_=100, value=self.config.data.get("overlay_opacity", 0.84) * 100,
                                       command=self._on_overlay_opacity, length=80)
        self.scale_opacity.pack(side=tk.LEFT, padx=2)
        # 位置信息
        of3 = ttk.Frame(overlay_frame); of3.pack(fill=tk.X, pady=(2, 0))
        self._tag(ttk.Label(of3, text="位置:", font=self.font_default), "default").pack(side=tk.LEFT)
        self.lbl_overlay_pos = ttk.Label(of3, text=self._overlay_pos_text(), font=self.font_tip, foreground="gray")
        self.lbl_overlay_pos.pack(side=tk.LEFT, padx=2)
        ttk.Button(of3, text="重置位置", command=self._on_overlay_reset).pack(side=tk.RIGHT)



        # 界面检测测试
        detect_test = ttk.LabelFrame(right, text="界面检测测试", padding=4)
        detect_test.pack(fill=tk.X, pady=(0, 4))
        dt = ttk.Frame(detect_test); dt.pack(fill=tk.X)
        ttk.Button(dt, text="🧪 检测升级界面", command=self._test_upgrade_detect).pack(side=tk.LEFT)

        # 箱子测试
        crate_test = ttk.LabelFrame(right, text="箱子测试", padding=4)
        crate_test.pack(fill=tk.X, pady=(0, 4))
        ct = ttk.Frame(crate_test); ct.pack(fill=tk.X)
        ttk.Button(ct, text="🧪 检测", command=self._test_crate_detect).pack(side=tk.LEFT, padx=(0, 2))
        ttk.Button(ct, text="📝 道具识别", command=self._test_item_ocr).pack(side=tk.LEFT, padx=2)

        # 商店测试
        shop_test = ttk.LabelFrame(right, text="商店测试", padding=4)
        shop_test.pack(fill=tk.X, pady=(0, 4))
        st0 = ttk.Frame(shop_test); st0.pack(fill=tk.X)
        ttk.Button(st0, text="🧪 检测道具", command=self._test_shop).pack(side=tk.LEFT, padx=(0, 2))
        st1 = ttk.Frame(shop_test); st1.pack(fill=tk.X, pady=(2, 0))
        ttk.Button(st1, text="🖱买1", width=5, command=lambda: self._test_shop_click("buy", 0)).pack(side=tk.LEFT, padx=(0, 1))
        ttk.Button(st1, text="🖱买2", width=5, command=lambda: self._test_shop_click("buy", 1)).pack(side=tk.LEFT, padx=1)
        ttk.Button(st1, text="🖱买3", width=5, command=lambda: self._test_shop_click("buy", 2)).pack(side=tk.LEFT, padx=1)
        ttk.Button(st1, text="🖱买4", width=5, command=lambda: self._test_shop_click("buy", 3)).pack(side=tk.LEFT, padx=1)
        ttk.Button(st1, text="🖱刷新", width=6, command=lambda: self._test_shop_click("refresh")).pack(side=tk.LEFT, padx=(6, 1))
        ttk.Button(st1, text="🖱出发", width=6, command=lambda: self._test_shop_click("leave")).pack(side=tk.LEFT, padx=1)

        # ====== 底部：提示 → 日志 → 分隔线（从下到上 pack） ======
        # 注意：side=BOTTOM 先 pack 的在下，后 pack 的在上

        # 提示（最底部）
        tip_parts = []
        k = self._get_shortcut_display
        tip_parts.append(f"{k('toggle')} 连续")
        tip_parts.append(f"{k('trigger_once')} 单次")
        tip_parts.append(f"{k('stop')} 停止")
        tip_parts.append(f"{k('test_crate')} 箱子检测")
        tip_parts.append(f"{k('test_item')} 道具识别")
        tip_parts.append(f"{k('pause')} 暂停")
        tip_parts.append(f"{k('test_upgrade_detect')} 升级检测")
        tip_parts.append("Shift+滚轮=横向滚动")
        tip_parts.append("拖拽分隔线调整日志高度")
        tip_text = "💡 " + " | ".join(tip_parts)
        self._tag(ttk.Label(main_frame,
            text=tip_text,
            font=self.font_tip, foreground="gray"
        ), "tip").pack(fill=tk.X, side=tk.BOTTOM, pady=(2, 0))

        # 日志
        self._log_h = 200
        log_frame = ttk.LabelFrame(main_frame, text="运行日志", padding=4)
        log_frame.pack(fill=tk.X, side=tk.BOTTOM)
        log_frame.pack_propagate(False)  # 允许手动设定高度
        log_frame.configure(height=self._log_h)
        self._log_frame = log_frame

        self.log_text = self._tag(scrolledtext.ScrolledText(
            log_frame, font=self.font_log, state="disabled", wrap=tk.WORD
        ), "log")
        self.log_text.pack(fill=tk.BOTH, expand=True)

        # 分隔线（日志上方，内容和日志之间）
        self._sep = tk.Frame(main_frame, height=6, cursor="sb_v_double_arrow",
                             bg="#999", relief=tk.GROOVE, bd=1)
        self._sep.pack(fill=tk.X, side=tk.BOTTOM, pady=(0, 0))
        self._sep.bind("<ButtonPress-1>", self._on_sep_press)
        self._sep.bind("<B1-Motion>", self._on_sep_drag)
        self._drag_start_y = 0
        self._log_start_h = 0

    # ---- 区域输入框 ↔ 配置 ----

    # ---- 日志分隔线拖拽 ----

    def _on_sep_press(self, event):
        self._drag_start_y = event.y_root
        self._log_start_h = self._log_h

    def _on_sep_drag(self, event):
        delta = self._drag_start_y - event.y_root  # 向上拖 = 减小日志
        new_h = self._log_start_h + delta
        # 限制范围：40px ~ 窗口高度-400px
        max_h = max(40, self.winfo_height() - 450)
        new_h = max(40, min(new_h, max_h))
        self._log_h = new_h
        self._log_frame.configure(height=new_h)

    def _on_sep_release(self, event):
        pass  # 高度已在 drag 中更新



    # ---- 商店 UI 辅助 ----

    def _make_region_row(self, parent: ttk.Frame, key: str, pick_key: str, delay: bool = False):
        """创建一行区域输入框 (X/Y/W/H + 框选按钮)，可选延迟。"""
        if not hasattr(self, '_shop_entries'):
            self._shop_entries = {}
        entries = [ttk.Entry(parent, width=5) for _ in range(4)]
        for i, label in enumerate(["X:", "Y:", "W:", "H:"]):
            ttk.Label(parent, text=label, font=self.font_default).pack(side=tk.LEFT)
            entries[i].pack(side=tk.LEFT, padx=(1, 4))
        cmd = self._open_region_selector
        cb = (lambda k=pick_key: (self.after(1000, cmd, k), self._append_log("[框选] 1 秒后进入框选界面…"))) if delay else (lambda k=pick_key: cmd(k))
        ttk.Button(parent, text="框", width=2, command=cb).pack(side=tk.LEFT)
        if delay:
            ttk.Label(parent, text="(点击后 1 秒延迟)", font=self.font_tip, foreground="gray").pack(side=tk.LEFT, padx=4)
        self._shop_entries[key] = entries

    def _make_coord_row(self, parent: ttk.Frame, key: str, pick_key: str, delay: bool = False):
        """创建一行坐标输入框 (X/Y + 拾取按钮)，可选延迟。"""
        if not hasattr(self, '_shop_entries'):
            self._shop_entries = {}
        entries = [ttk.Entry(parent, width=5) for _ in range(2)]
        ttk.Label(parent, text="X:", font=self.font_default).pack(side=tk.LEFT)
        entries[0].pack(side=tk.LEFT, padx=(1, 4))
        ttk.Label(parent, text="Y:", font=self.font_default).pack(side=tk.LEFT)
        entries[1].pack(side=tk.LEFT, padx=(1, 0))
        cmd = self._open_region_selector
        cb = (lambda k=pick_key: (self.after(1000, cmd, k), self._append_log("[框选] 1 秒后进入框选界面…"))) if delay else (lambda k=pick_key: cmd(k))
        ttk.Button(parent, text="取", width=2, command=cb).pack(side=tk.LEFT)
        if delay:
            ttk.Label(parent, text="(点击后 1 秒延迟)", font=self.font_tip, foreground="gray").pack(side=tk.LEFT, padx=4)
        self._shop_entries[key] = entries

    def _load_shop_entries(self):
        """从配置加载商店输入框。"""
        if not hasattr(self, '_shop_entries'):
            return
        # 商店检测区域
        for entry_key, cfg_key in [("shop_detect", "shop_detect_region"),
                                    ("upgrade_detect", "upgrade_detect_region")]:
            r = self.config.data.get(cfg_key, {})
            if entry_key in self._shop_entries:
                for i, k in enumerate(["left", "top", "width", "height"]):
                    self._shop_entries[entry_key][i].delete(0, tk.END)
                    self._shop_entries[entry_key][i].insert(0, str(r.get(k, 0)))
        # 槽位区域
        slot_regions = self.config.data.get("shop_slot_regions", [])
        for si in range(4):
            key = f"shop_slot_{si}"
            if key not in self._shop_entries:
                continue
            sr = slot_regions[si] if si < len(slot_regions) else {}
            for i, k in enumerate(["left", "top", "width", "height"]):
                self._shop_entries[key][i].delete(0, tk.END)
                self._shop_entries[key][i].insert(0, str(sr.get(k, 0)))
        # 购买坐标
        buy_coords = self.config.data.get("shop_slot_buy_coords", [])
        for si in range(4):
            key = f"shop_buy_{si}"
            if key not in self._shop_entries:
                continue
            bc = buy_coords[si] if si < len(buy_coords) else {}
            self._shop_entries[key][0].delete(0, tk.END)
            self._shop_entries[key][0].insert(0, str(bc.get("x", 0)))
            self._shop_entries[key][1].delete(0, tk.END)
            self._shop_entries[key][1].insert(0, str(bc.get("y", 0)))
        # 刷新坐标
        rc = self.config.data.get("shop_refresh_coord", {})
        for key in ["shop_refresh", "shop_leave"]:
            if key not in self._shop_entries:
                continue
            coord = rc if key == "shop_refresh" else (self.config.data.get("shop_leave_coord") or {})
            self._shop_entries[key][0].delete(0, tk.END)
            self._shop_entries[key][0].insert(0, str(coord.get("x", 0)))
            self._shop_entries[key][1].delete(0, tk.END)
            self._shop_entries[key][1].insert(0, str(coord.get("y", 0)))
        # 升级点击坐标
        upgrade_click = self.config.data.get("upgrade_click_coord") or {}
        if "upgrade_click" in self._shop_entries:
            self._shop_entries["upgrade_click"][0].delete(0, tk.END)
            self._shop_entries["upgrade_click"][0].insert(0, str(upgrade_click.get("x", 0)))
            self._shop_entries["upgrade_click"][1].delete(0, tk.END)
            self._shop_entries["upgrade_click"][1].insert(0, str(upgrade_click.get("y", 0)))

    def _crate_region_to_entries(self):
        """将箱子/道具区域配置加载到输入框（通过 _shop_entries）。"""
        if not hasattr(self, '_shop_entries'):
            return
        for entry_key, cfg_key in [
            ("crate_detect_region_", "crate_region"),
            ("item_detect_region_", "item_region"),
        ]:
            if entry_key not in self._shop_entries:
                continue
            r = self.config.data.get(cfg_key, {"left": 0, "top": 0, "width": 100, "height": 50})
            entries = self._shop_entries[entry_key]
            entries[0].delete(0, tk.END); entries[0].insert(0, str(r.get("left", 0)))
            entries[1].delete(0, tk.END); entries[1].insert(0, str(r.get("top", 0)))
            entries[2].delete(0, tk.END); entries[2].insert(0, str(r.get("width", 100)))
            entries[3].delete(0, tk.END); entries[3].insert(0, str(r.get("height", 50)))

    # ---- 区域框选 ----

    def _open_region_selector(self, region_key: str = "crate"):
        """
        打开全屏框选工具。
        region_key 决定存储目标：
          "crate" / "item" → rect 存入对应 _region
          "shop_detect_region" → shop_detect_region
          "shop_slot_N" → shop_slot_regions[N]
          "shop_buy_N" → shop_slot_buy_coords[N] (取中心点)
          "shop_refresh" / "shop_leave" → 对应 coord (取中心点)
        """
        label = region_key.replace("_", " ")

        self.iconify()
        time.sleep(0.3)

        def _on_selected(rect):
            self.deiconify()
            if rect is None:
                self._append_log(f"[框选-{label}] 已取消")
                return

            # 判断是区域还是坐标
            is_coord = region_key.startswith("shop_buy_") or region_key in ("shop_refresh", "shop_leave", "upgrade_click")

            if is_coord:
                # 取框选矩形的中心点
                coord = {"x": rect["left"] + rect["width"] // 2,
                         "y": rect["top"] + rect["height"] // 2}
                if region_key.startswith("shop_buy_"):
                    idx = int(region_key.split("_")[-1])
                    coords = self.config.data.setdefault("shop_slot_buy_coords", [{}, {}, {}, {}])
                    if idx < len(coords):
                        coords[idx] = coord
                elif region_key == "shop_refresh":
                    self.config.data["shop_refresh_coord"] = coord
                elif region_key == "shop_leave":
                    self.config.data["shop_leave_coord"] = coord
                elif region_key == "upgrade_click":
                    self.config.data["upgrade_click_coord"] = coord
                self._append_log(f"[框选-{label}] 坐标: ({coord['x']}, {coord['y']})")
            else:
                # 区域
                if region_key == "crate":
                    self.config.data["crate_region"] = rect
                elif region_key == "item":
                    self.config.data["item_region"] = rect
                elif region_key == "shop_detect_region":
                    self.config.data["shop_detect_region"] = rect
                elif region_key == "upgrade_detect":
                    self.config.data["upgrade_detect_region"] = rect
                elif region_key.startswith("shop_slot_"):
                    idx = int(region_key.split("_")[-1])
                    slots = self.config.data.setdefault("shop_slot_regions", [{}, {}, {}, {}])
                    if idx < len(slots):
                        slots[idx] = rect
                self._append_log(f"[框选-{label}] {rect['width']}x{rect['height']}")

            self.config.save()
            # 刷新所有输入框
            if region_key in ("crate", "item"):
                self._crate_region_to_entries()
            else:
                self._load_shop_entries()

            self.lift()
            self.focus_force()

        rs = RegionSelector(self, callback=_on_selected)
        rs.grab_set()

    # ---- 测试按钮 ----

    def _test_upgrade_detect(self):
        """测试升级界面检测。"""
        try:
            region = self.config.data.get("upgrade_detect_region")
            if not region:
                messagebox.showwarning("未设置", "请先框选升级检测区域")
                return
            text = self.upgrader.reader.ocr_text(region, lang="chi_sim+eng")
            in_upgrade = self.upgrader._is_on_upgrade_screen()
            self._append_log(f"[升级检测] OCR=[{text}] 升级界面={'✓' if in_upgrade else '✗'}")
            msg = f"OCR 文本: [{text}] (长度{len(text)})\n\n{'✓ 是升级界面' if in_upgrade else '✗ 非升级界面'}"
            messagebox.showinfo("升级检测结果", msg)
        except Exception as e:
            messagebox.showerror("错误", f"{e}")

    def _test_crate_detect(self):
        """测试箱子检测：检查 crate_region 中是否识别到'属性'。"""
        try:
            reader = ScreenReader(self.config)
            crate_region = self.config.data.get("crate_region")
            if not crate_region:
                messagebox.showwarning("未设置", "请先框选箱子识别区域")
                return
            text = reader.ocr_text(crate_region, lang="chi_sim+eng")
            found = "属性" in text
            self._append_log(f"[箱子测试] 区域OCR=[{text}] | 检测\"属性\"={'✓' if found else '✗'}")
            msg = f"OCR 文本: [{text}]\n\n检测到\"属性\": {'是 - 箱子界面' if found else '否 - 非箱子界面'}"
            messagebox.showinfo("箱子检测结果", msg)
        except Exception as e:
            messagebox.showerror("错误", f"箱子检测失败:\n{e}")

    def _open_shortcut_config(self):
        """打开快捷键设置窗口（期间全局快捷键暂停）。"""
        self._shortcuts_suspended = True
        win = ShortcutConfigWindow(self, self.config, self.font_default, self.font_bold)
        win._append_log = self._append_log
        win.transient(self)
        win.bind("<Destroy>", lambda e: setattr(self, '_shortcuts_suspended', False))

    def _test_item_ocr(self):
        """测试道具识别：读取 item_region 中的道具名，并模拟匹配期望道具。"""
        try:
            reader = ScreenReader(self.config)
            item_region = self.config.data.get("item_region")
            if not item_region:
                messagebox.showwarning("未设置", "请先框选道具名识别区域")
                return
            raw = reader.ocr_text(item_region, lang="chi_sim+eng")

            # 与 _handle_crate 中相同的提取逻辑
            lines = [l.strip() for l in raw.split('\n') if l.strip()]
            ui_keywords = ['属性', '生命', '攻击', '速度', '范围', '伤害', '暴击',
                          '护甲', '闪避', '收获', '幸运', '等级', '波次', '材料']
            item_lines = [
                l for l in lines
                if len(l) >= 2 and not l.isdigit()
                and not any(kw in l for kw in ui_keywords)
            ]
            item_name = item_lines[0] if item_lines else (lines[0] if lines else "")

            # 匹配期望道具
            desired = self.config.get("desired_items")
            matched = ""
            if desired and item_name:
                for d in desired:
                    if d.strip() and d.strip() in item_name:
                        matched = d.strip()
                        break

            if matched:
                verdict = f"🎁 匹配 [{matched}] → 会按空格拾取"
                self._append_log(f"[道具测试] 名=[{item_name}] 匹配=[{matched}] → 拾取")
            elif item_name:
                verdict = f"🗑 未匹配期望列表 → 会按 F 跳过"
                self._append_log(f"[道具测试] 名=[{item_name}] 未匹配 → 跳过")
            else:
                verdict = "⚠ 未识别到道具名 → 会按 F 跳过"
                self._append_log(f"[道具测试] 未识别到道具名 → 跳过")

            self._append_log(f"[道具测试] 原始OCR({len(raw)}字)=[{raw[:200]}]")

            # 显示截取的区域图片
            bgr = reader.grab_region_image(item_region)
            img = Image.fromarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
            self._show_image(img, "道具区域截图")

            msg = (
                f"OCR 识别文本 ({len(raw)} 字):\n{raw[:300]}\n\n"
                f"提取道具名: [{item_name}]\n"
                f"期望道具: {desired}\n\n"
                f"判定: {verdict}"
            )
            messagebox.showinfo("道具识别结果", msg)
        except Exception as e:
            messagebox.showerror("错误", f"道具识别失败:\n{e}")

    def _test_shop(self):
        """测试商店检测：OCR 四个槽位，模拟判断逻辑（假设箱子阶段未拿到沙漏）。"""
        try:
            reader = ScreenReader(self.config)

            # 1) 检测是否在商店
            detect_region = self.config.data.get("shop_detect_region")
            shop_text = ""
            in_shop = False
            if detect_region:
                shop_text = reader.ocr_text(detect_region, lang="chi_sim+eng")
                in_shop = "商店" in shop_text

            # 2) OCR 四个槽位
            slot_regions = self.config.data.get("shop_slot_regions", [])
            slot_names: list[str] = []
            for i, region in enumerate(slot_regions):
                if i >= 4:
                    break
                text = reader.ocr_text(region, lang="chi_sim+eng")
                slot_names.append(text)

            # 3) 匹配逻辑（假设箱子阶段没有沙漏）
            target = self.config.data.get("shop_target_item", "沙漏")
            secondary = self.config.data.get("shop_secondary_items", [])

            found_target = -1
            found_secondary: list[tuple[int, str]] = []
            for i, name in enumerate(slot_names):
                if target in name:
                    found_target = i
                for item in secondary:
                    if item in name:
                        found_secondary.append((i, item))

            # 4) 判断操作
            if not in_shop:
                action = "⚠ 不在商店界面（未检测到\"商店\"）"
                verdict = "跳过商店"
            elif found_target >= 0:
                action = f"购买槽位{found_target+1} [{slot_names[found_target]}] → 点「出发」"
                verdict = f"🎯 找到目标 [{target}]，购买后出发"
            elif found_secondary:
                items_str = "、".join(f"[{slot_names[i]}]" for i, _ in found_secondary)
                action = f"购买: {items_str} → 点「刷新」继续"
                verdict = f"🔄 仅有次级道具，购买后继续刷新"
            else:
                action = "点「刷新」继续查找"
                verdict = "🔄 无匹配道具，刷新"

            # 5) 日志 + 弹窗
            self._append_log(f"[商店测试] 在商店={'✓' if in_shop else '✗'} OCR=[{shop_text}]")
            for i, name in enumerate(slot_names):
                matched = ""
                if i == found_target:
                    matched = f" ★目标[{target}]"
                for si, item in found_secondary:
                    if si == i:
                        matched += f" ☆次级[{item}]"
                self._append_log(f"[商店测试]   槽{i+1}: [{name}]{matched}")
            self._append_log(f"[商店测试] 判定: {verdict}")

            slots_display = "\n".join(
                f"  槽{i+1}: [{name}]" +
                (f" ★目标[{target}]" if i == found_target else "") +
                ("".join(f" ☆次级[{item}]" for si, item in found_secondary if si == i))
                for i, name in enumerate(slot_names)
            )
            msg = (
                f"商店界面: {'是' if in_shop else '否'} [{shop_text}]\n\n"
                f"识别道具:\n{slots_display}\n\n"
                f"目标: {target}  次级: {secondary}\n"
                f"假设: 箱子阶段未拿到{target}\n\n"
                f"判定: {verdict}\n"
                f"操作: {action}"
            )
            messagebox.showinfo("商店测试结果", msg)
        except Exception as e:
            messagebox.showerror("错误", f"商店测试失败:\n{e}")

    def _test_shop_click(self, action: str, slot_idx: int = -1):
        """测试鼠标点击：action="buy"|"refresh"|"leave"。slot_idx 仅对 buy 有效。"""
        coords = self.config.data.get("shop_slot_buy_coords", [])
        refresh = self.config.data.get("shop_refresh_coord", {})
        leave = self.config.data.get("shop_leave_coord", {})

        if action == "buy" and 0 <= slot_idx < len(coords):
            c = coords[slot_idx]
            x, y = c.get("x", 0), c.get("y", 0)
            label = f"购买槽{slot_idx+1}"
        elif action == "refresh":
            x, y = refresh.get("x", 0), refresh.get("y", 0)
            label = "刷新"
        elif action == "leave":
            x, y = leave.get("x", 0), leave.get("y", 0)
            label = "出发"
        else:
            return

        self._append_log(f"[点击测试] {label}: ({x}, {y})")
        # 等 0.3 秒让用户切到游戏窗口
        time.sleep(0.3)
        self.upgrader.mouse.position = (x, y)
        time.sleep(0.05)
        self.upgrader.mouse.click(Button.left, 1)

    @staticmethod
    def _show_image(img: Image.Image, title: str):
        """在新窗口显示 PIL 图像（放大显示）。"""
        w, h = img.size
        scale = min(400 / w, 300 / h, 4.0)
        new_w, new_h = int(w * scale), int(h * scale)
        display = img.resize((new_w, new_h), Image.NEAREST)

        win = tk.Toplevel()
        win.title(title)
        win.resizable(False, False)

        from PIL import ImageTk
        tk_img = ImageTk.PhotoImage(display)

        label = tk.Label(win, image=tk_img, bg="black")
        label.image = tk_img  # 保持引用
        label.pack()

        win.focus_force()

    # ---- 字体大小回调 ----

    def _on_font_size_change(self):
        """字体大小 Spinbox 改变时的回调。"""
        try:
            fs = int(self.spin_font.get())
            if 8 <= fs <= 36:
                self._apply_font_size(fs)
        except ValueError:
            pass

    # ---- 保存 & 启停 ----

    def _save_config(self):
        """保存配置到文件。"""
        # 连续模式运行中保存会重建实例，先停止，避免旧工作线程继续执行
        if self.upgrader.is_running:
            self.upgrader.stop_continuous()
            self._append_log("  ℹ 连续模式已停止，保存后请重新启动以应用新配置")
        try:
            delay = int(self.entry_delay.get())
            if delay < 25:
                raise ValueError("间隔不能小于 25ms")
        except ValueError:
            messagebox.showwarning("输入错误", "间隔必须是 >= 25 的整数")
            return

        self.config.data["delay_between_upgrades_ms"] = delay
        try:
            val = int(self.entry_upgrade_max.get())
            if val < 0:
                raise ValueError("上限不能为负数")
            self.config.data["upgrade_max"] = val
        except ValueError as e:
            messagebox.showwarning("输入错误", f"升级上限格式错误: {e}\n请输入 >= 0 的整数（0=不限）")
            return

        self.config.data["font_size"] = int(self.spin_font.get())

        # 箱子设置
        self.config.data["crate_enabled"] = self.crate_enabled_var.get()
        self.config.data["crate_auto_skip"] = self.crate_auto_skip_var.get()
        self.config.data["desired_items"] = [
            s.strip() for s in self.entry_desired_items.get().split(",") if s.strip()
        ]
        try:
            crate_delay = int(self.entry_crate_delay.get())
            if crate_delay < 25:
                crate_delay = 25
            self.config.data["crate_delay_ms"] = crate_delay
        except ValueError:
            self.config.data["crate_delay_ms"] = 800

        def _read_region_from_entries(entries):
            return {
                "left": int(entries[0].get()),
                "top": int(entries[1].get()),
                "width": int(entries[2].get()),
                "height": int(entries[3].get()),
            }

        # 箱子区域（从 _shop_entries 读取）
        se = self._shop_entries if hasattr(self, '_shop_entries') else {}
        for entry_key, cfg_key in [("crate_detect_region_", "crate_region"),
                                    ("item_detect_region_", "item_region")]:
            if entry_key in se:
                self.config.data[cfg_key] = _read_region_from_entries(se[entry_key])

        # 商店设置
        self.config.data["shop_enabled"] = self.shop_enabled_var.get()
        self.config.data["shop_auto_skip"] = self.shop_auto_skip_var.get()
        self.config.data["shop_target_item"] = self.entry_shop_target.get().strip()
        self.config.data["shop_secondary_items"] = [
            s.strip() for s in self.entry_shop_secondary.get().split(",") if s.strip()
        ]
        try:
            self.config.data["shop_max_refreshes"] = int(self.entry_shop_refreshes.get())
        except ValueError:
            self.config.data["shop_max_refreshes"] = 2000
        try:
            self.config.data["shop_delay_ms"] = int(self.entry_shop_delay.get())
        except ValueError:
            self.config.data["shop_delay_ms"] = 500

        if hasattr(self, '_shop_entries'):
            se = self._shop_entries
            # 商店检测区域
            if "shop_detect" in se:
                self.config.data["shop_detect_region"] = _read_region_from_entries(se["shop_detect"])
            if "upgrade_detect" in se:
                self.config.data["upgrade_detect_region"] = _read_region_from_entries(se["upgrade_detect"])
            # 槽位区域
            slot_regions = []
            for i in range(4):
                key = f"shop_slot_{i}"
                if key in se:
                    slot_regions.append(_read_region_from_entries(se[key]))
            if slot_regions:
                self.config.data["shop_slot_regions"] = slot_regions
            # 购买坐标
            buy_coords = []
            for i in range(4):
                key = f"shop_buy_{i}"
                if key in se:
                    buy_coords.append({"x": int(se[key][0].get()), "y": int(se[key][1].get())})
            if buy_coords:
                self.config.data["shop_slot_buy_coords"] = buy_coords
            # 刷新 / 出发坐标
            for key, cfg_key in [("shop_refresh", "shop_refresh_coord"),
                                 ("shop_leave", "shop_leave_coord"),
                                 ("upgrade_click", "upgrade_click_coord")]:
                if key in se:
                    self.config.data[cfg_key] = {
                        "x": int(se[key][0].get()), "y": int(se[key][1].get())
                    }

        self.config.save()

        # 重建 upgrader 使配置生效（必须传全部回调，否则浮窗失效）
        self.upgrader = AutoUpgrader(
            self.config,
            log_callback=self._append_log,
            status_callback=self._update_overlay,
            history_callback=self._overlay_history,
            history_clear_callback=self._clear_overlay_history
        )

        self._append_log("[配置] 已保存 ✓")
        self._update_status()
        messagebox.showinfo("保存成功", "配置已保存")

    def _toggle(self):
        """按钮：切换连续模式。"""
        if self.overlay.is_preview_active:
            self._on_overlay_exit_settings()
        if self.upgrader.is_running:
            self.upgrader.stop_continuous()
        else:
            self.upgrader.start_continuous()
        self._update_status()

    def _trigger_once(self):
        """按钮：触发单次升级。"""
        if self.overlay.is_preview_active:
            self._on_overlay_exit_settings()
        if self.upgrader.is_running:
            self._append_log("  ℹ 连续模式已开启，请先关闭")
            return
        self._append_log("[手动触发] 执行单次升级…")
        def _run():
            self.upgrader.trigger_once()
        Thread(target=_run, daemon=True).start()

    def _on_close(self):
        """关闭窗口时清理。"""
        if self.upgrader.is_running:
            self.upgrader.stop_continuous()
        if self._hotkey_listener and self._hotkey_listener.is_alive():
            self._hotkey_listener.stop()
        self.overlay.hide()
        self.overlay.destroy()
        self.destroy()


# ===================================================================
# 入口
# ===================================================================

def main():
    app = MainApp()
    app.mainloop()


if __name__ == "__main__":
    main()
