"""グラウンド空き監視 メイン処理
各区のサイトから空きコマを取得 → 前回結果と比較 → 新規空きを Discord へ通知。
"""
import datetime as dt
import sys
import time
import traceback

import yaml
from playwright.sync_api import sync_playwright

from . import util
from .sites import chuo, ota, shinagawa, user_home

SITE_MODULES = {
    "chuo": (chuo.fetch, chuo.BOOKING_URL),
    "shinagawa": (shinagawa.fetch, shinagawa.BOOKING_URL),
    "ota": (ota.fetch, ota.BOOKING_URL),
    "sumida": (user_home.fetch, None),   # URLはconfigのbase_url
    "suginami": (user_home.fetch, None),
}

WD_JP = "月火水木金土日"


def fmt_date(iso: str) -> str:
    d = dt.date.fromisoformat(iso)
    return f"{d.month}/{d.day}({WD_JP[d.weekday()]})"


def main() -> int:
    with open("config.yaml", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    filters = config["filters"]
    dates = util.target_dates(filters["days"], filters.get("months_ahead", 1))
    print(f"[info] 監視対象日: {len(dates)}日 ({dates[0]}〜{dates[-1]})")

    state = util.load_state()
    notify_lines: list[str] = []
    errors: list[str] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        for key, site_cfg in config["sites"].items():
            if not site_cfg.get("enabled", False):
                continue
            if key not in SITE_MODULES:
                print(f"[warn] 未知のサイトキー: {key}")
                continue
            fetch, booking_url = SITE_MODULES[key]
            name = site_cfg.get("name", key)
            print(f"[info] === {name} ===")
            try:
                slots = fetch(browser, site_cfg, filters, dates)
                print(f"[info] {name}: 空きコマ {len(slots)}件")
                available = {f"{fac}|{d}|{slot}" for fac, d, slot in slots}
                new_keys = util.diff_new(state, key, available)
                if new_keys:
                    url = booking_url or site_cfg.get("base_url", "")
                    notify_lines.append(f"**【{name}】新しい空きが出ました！**")
                    for k in new_keys:
                        fac, d, slot = k.split("|", 2)
                        notify_lines.append(f"・{fac}  {fmt_date(d)}  {slot}")
                    if url:
                        notify_lines.append(f"→ 予約: {url}")
                    notify_lines.append("")
            except util.SkipSite as e:
                print(f"[skip] {name}: {e}")
            except Exception as e:  # noqa: BLE001
                print(f"[error] {name}: {e}")
                traceback.print_exc()
                errors.append(name)
            time.sleep(3)  # サイト間で間隔を空ける（マナー）
        browser.close()

    # エラー通知は6時間に1回まで
    if errors:
        now = dt.datetime.now(util.JST)
        last = state.get("last_error_notify")
        ok_to_notify = True
        if last:
            ok_to_notify = (now - dt.datetime.fromisoformat(last)) > dt.timedelta(hours=6)
        if ok_to_notify:
            notify_lines.append(
                f"⚠️ 取得エラー: {', '.join(errors)}（Actionsのdebug成果物を確認）"
            )
            state["last_error_notify"] = now.isoformat(timespec="seconds")

    if notify_lines:
        util.notify_discord(notify_lines)
        print("[info] Discord通知を送信しました")
    else:
        print("[info] 新規空きなし")

    util.save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
