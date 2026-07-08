"""大田区 うぐいすネット (yoyaku.city.ota.tokyo.jp) CGI型。
※このサイトだけ事前にブラウザでの実画面確認ができていないため、
  初回実行時に debug/ の成果物（HTML/スクリーンショット）を見て
  セレクタを微調整する前提の実装。
流れ(想定): トップ → ログインせずに空き状況を検索 → 施設の空き照会
→ カテゴリで検索 → 「大田スタジアム」 → 選択した条件で次へ → 空き状況表。
"""
import datetime as dt
import re

from ..util import UA, dump_debug, start_hour_ok

HOME = "https://www.yoyaku.city.ota.tokyo.jp/eshisetsu/menu/Welcome.cgi"
BOOKING_URL = "https://www.yoyaku.city.ota.tokyo.jp/"


def fetch(browser, cfg, filters, dates) -> set[tuple[str, str, str]]:
    date_set = {d.isoformat() for d in dates}
    results: set[tuple[str, str, str]] = set()
    ctx = browser.new_context(user_agent=UA, locale="ja-JP")
    page = ctx.new_page()
    try:
        page.goto(HOME, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)

        # ログインせずに空き状況を検索
        page.get_by_text("ログインせずに空き状況を検索").first.click()
        page.wait_for_timeout(1500)

        # 申込の種類: 施設の空き照会／予約申込
        page.locator('input[name="yoyakuMode"]').first.check()
        page.wait_for_timeout(500)

        # 検索条件: カテゴリで検索 → カテゴリ名
        try:
            page.get_by_text("カテゴリで検索", exact=True).first.click()
            page.wait_for_timeout(800)
        except Exception:  # noqa: BLE001
            pass
        page.get_by_text(cfg.get("category", "大田スタジアム"), exact=True).first.click()
        page.wait_for_timeout(800)

        page.get_by_text("選択した条件で次へ").first.click()
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(3000)

        # ここから先は施設一覧 or 空き状況カレンダーのはず。
        # まず施設一覧なら先頭の施設を選んで進む
        _maybe_advance(page)

        # 週送りしながら対象期間の表を読む
        end = max(dates)
        for _ in range(12):  # 最大12週分
            found_dates = _parse_grid(page, cfg, filters, date_set, results)
            if found_dates and max(found_dates) >= end.isoformat():
                break
            if not _click_next_week(page):
                break
        if not results:
            # 何も取れなかった場合は解析用に保存（空きゼロの可能性もある）
            dump_debug(page, "ota_last_page")
        return results
    except Exception:
        dump_debug(page, "ota_error")
        raise
    finally:
        ctx.close()


def _maybe_advance(page):
    """施設選択の中間ページが挟まる場合に前へ進める（ベストエフォート）"""
    for label in ["大田スタジアム", "次へ"]:
        try:
            el = page.get_by_text(label, exact=False).first
            if el.count() if hasattr(el, "count") else 0:
                pass
            el.click(timeout=3000)
            page.wait_for_timeout(2000)
        except Exception:  # noqa: BLE001
            pass


def _parse_grid(page, cfg, filters, date_set, results):
    """表を総当たりで解析: 日付ヘッダ + ○/△セル + 時間帯行/列"""
    found_dates = []
    tables = page.evaluate(
        """() => [...document.querySelectorAll('table')].map(t =>
             [...t.rows].map(r => [...r.cells].map(c => c.textContent.trim())))"""
    )
    body = page.inner_text("body")
    ym = re.search(r"(\d{4})年\s*(\d{1,2})月", body)
    year = int(ym.group(1)) if ym else dt.date.today().year

    for rows in tables:
        if not rows or len(rows) < 2:
            continue
        text = " ".join(" ".join(r) for r in rows)
        if not re.search(r"[○△]", text):
            continue
        # ヘッダ行から日付列を探す（「M/D」or「D(曜)」形式に対応）
        header = rows[0]
        col_dates = {}
        for i, c in enumerate(header):
            m1 = re.search(r"(\d{1,2})/(\d{1,2})", c)
            m2 = re.search(r"(\d{1,2})月(\d{1,2})日", c)
            if m1:
                mon, day = int(m1.group(1)), int(m1.group(2))
            elif m2:
                mon, day = int(m2.group(1)), int(m2.group(2))
            else:
                continue
            y = year if mon >= dt.date.today().month else year + 1
            try:
                col_dates[i] = dt.date(y, mon, day).isoformat()
            except ValueError:
                continue
        if not col_dates:
            continue
        for row in rows[1:]:
            slot_label = row[0] if row else ""
            for i, cell in enumerate(row):
                if i not in col_dates or cell not in ("○", "△"):
                    continue
                d = col_dates[i]
                found_dates.append(d)
                if d not in date_set:
                    continue
                label = slot_label if re.search(r"\d", slot_label) else "要確認"
                if label == "要確認" or start_hour_ok(
                    label, filters["start_hour_min"], filters["start_hour_max"]
                ):
                    results.add((cfg.get("category", "大田スタジアム"), d, label))
    return found_dates


def _click_next_week(page) -> bool:
    for name in ["翌週", "次の期間", "次週", "翌月"]:
        try:
            page.get_by_text(name, exact=False).first.click(timeout=3000)
            page.wait_for_timeout(2500)
            return True
        except Exception:  # noqa: BLE001
            continue
    return False
