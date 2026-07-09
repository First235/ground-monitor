"""共通ユーティリティ: 日付計算・状態管理・Discord通知・デバッグ保存"""
import calendar
import datetime as dt
import json
import os
import re

import jpholiday
import requests

JST = dt.timezone(dt.timedelta(hours=9))
UA = "ground-monitor (personal availability checker; polite; contact via repo)"
STATE_FILE = "state.json"
DEBUG_DIR = "debug"


class SkipSite(Exception):
    """メンテナンス中など、エラーではないが今回スキップする場合に投げる"""


def today_jst() -> dt.date:
    return dt.datetime.now(JST).date()


def is_target_day(d: dt.date, days: list[str]) -> bool:
    if "sat" in days and d.weekday() == 5:
        return True
    if "sun" in days and d.weekday() == 6:
        return True
    if "holiday" in days and jpholiday.is_holiday(d):
        return True
    return False


def target_dates(days: list[str], months_ahead: int = 1) -> list[dt.date]:
    """今日〜(当月+months_ahead)月末までの土日祝リスト"""
    start = today_jst()
    y, m = start.year, start.month
    for _ in range(months_ahead):
        m += 1
        if m > 12:
            y, m = y + 1, 1
    end = dt.date(y, m, calendar.monthrange(y, m)[1])
    out = []
    d = start
    while d <= end:
        if is_target_day(d, days):
            out.append(d)
        d += dt.timedelta(days=1)
    return out


def start_hour_ok(label: str, hmin: int, hmax: int) -> bool:
    """'10時から12時まで' '13:00～' '9:00～11:00' 等から開始時を取り時間帯判定"""
    m = re.search(r"(\d{1,2})\s*[:時]", label)
    if not m:
        return False
    return hmin <= int(m.group(1)) <= hmax


# ---------------- 状態管理 ----------------

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"sites": {}}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)


def diff_new(state: dict, site_key: str, available: set[str]) -> list[str]:
    """新しく空きになったキーを返す。初回はベースライン保存のみで通知なし"""
    site = state["sites"].setdefault(site_key, {})
    first_run = "available" not in site
    old = set(site.get("available", []))
    site["available"] = sorted(available)
    site["last_ok"] = dt.datetime.now(JST).isoformat(timespec="seconds")
    return [] if first_run else sorted(available - old)


# ---------------- Discord ----------------

def notify_discord(lines: list[str]) -> None:
    url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not url:
        print("[warn] DISCORD_WEBHOOK_URL 未設定。通知内容:")
        print("\n".join(lines))
        return
    # 2000文字制限があるので分割送信
    buf = ""
    chunks = []
    for line in lines:
        if len(buf) + len(line) + 1 > 1900:
            chunks.append(buf)
            buf = ""
        buf += line + "\n"
    if buf:
        chunks.append(buf)
    for c in chunks:
        r = requests.post(url, json={"content": c}, timeout=15)
        r.raise_for_status()


# ---------------- デバッグ ----------------

def dump_debug(page, name: str) -> None:
    """失敗時にHTMLとスクリーンショットを debug/ に保存（Actionsの成果物になる）
    page には Page でも Frame でも渡せる"""
    os.makedirs(DEBUG_DIR, exist_ok=True)
    try:
        html = page.evaluate("() => document.documentElement.outerHTML")
        with open(f"{DEBUG_DIR}/{name}.html", "w", encoding="utf-8") as f:
            f.write(html)
        print(f"[debug] saved {DEBUG_DIR}/{name}.html")
    except Exception as e:  # noqa: BLE001
        print(f"[debug] html dump failed: {e}")
    try:
        shooter = page.page if hasattr(page, "page") else page  # Frame -> Page
        shooter.screenshot(path=f"{DEBUG_DIR}/{name}.png", full_page=True)
        print(f"[debug] saved {DEBUG_DIR}/{name}.png")
    except Exception as e:  # noqa: BLE001
        print(f"[debug] screenshot failed: {e}")
