"""品川区 しせつよやく (cm9.eprs.jp)
天王洲公園・八潮北公園の野球場。
検索結果ページの #week-info テーブル（時間帯×日付、1か月表示）を読む。
セルの img alt が「空き」なら空きコマ。
"""
import datetime as dt
import re
import time

from ..util import UA, dump_debug, start_hour_ok

HOME = "https://www.cm9.eprs.jp/shinagawa/web/"
BOOKING_URL = HOME


def fetch(browser, cfg, filters, dates) -> set[tuple[str, str, str]]:
    date_set = {d.isoformat() for d in dates}
    results: set[tuple[str, str, str]] = set()
    ctx = browser.new_context(user_agent=UA, locale="ja-JP")
    page = ctx.new_page()
    try:
        page.goto(HOME, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2000)

        # ---- 検索条件を設定 ----
        # いつ: 「1か月」クイックボタン（開始日=今日、期間=1か月になる）
        page.get_by_role("button", name="1か月").first.click()
        page.wait_for_timeout(500)

        # 曜日: 土・日・祝 をON（トグルボタン）
        for wd in ["土", "日", "祝"]:
            btn = page.get_by_role("button", name=wd, exact=True).first
            btn.click()
            page.wait_for_timeout(300)

        # どこで: 施設（館）を選択
        for venue in cfg.get("venues", []):
            page.get_by_text(venue, exact=True).first.click()
            page.wait_for_timeout(300)

        # 何をする: 利用目的
        page.get_by_text(cfg.get("purpose", "野球"), exact=True).first.click()
        page.wait_for_timeout(300)

        # 検索
        page.get_by_role("button", name="検索", exact=True).click()
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(2000)

        # ---- 結果ページ: 館×施設の組み合わせを順に読む ----
        room_re = re.compile(cfg.get("room_filter", "野球"))
        selects = page.locator("select")
        n_sel = selects.count()
        if n_sel >= 2:
            venue_sel, room_sel = selects.nth(0), selects.nth(1)
            venue_opts = venue_sel.locator("option").all_text_contents()
            for v in venue_opts:
                v = v.strip()
                if not v or v not in cfg.get("venues", [v]):
                    continue
                venue_sel.select_option(label=v)
                page.wait_for_timeout(1500)
                room_opts = room_sel.locator("option").all_text_contents()
                for r in room_opts:
                    r = r.strip()
                    if not r or not room_re.search(r):
                        continue
                    room_sel.select_option(label=r)
                    page.wait_for_timeout(1500)
                    _read_months(page, f"{v} {r}", date_set, filters, results)
        else:
            # ドロップダウンが見つからない場合は表示中の1組だけ読む
            dump_debug(page, "shinagawa_no_select")
            _read_months(page, "品川施設", date_set, filters, results)
        return results
    except Exception:
        dump_debug(page, "shinagawa_error")
        raise
    finally:
        ctx.close()


def _read_months(page, label, date_set, filters, results):
    """今月と翌月の #week-info を読む"""
    _ensure_week_open(page)
    _parse_week_table(page, label, date_set, filters, results)
    # 翌月へ
    try:
        page.get_by_role("button", name=re.compile("翌月")).first.click()
        page.wait_for_timeout(2000)
        _parse_week_table(page, label, date_set, filters, results)
        # 戻しておく（次の施設のため）
        page.get_by_role("button", name=re.compile("前月")).first.click()
        page.wait_for_timeout(1500)
    except Exception as e:  # noqa: BLE001
        print(f"[shinagawa] 翌月表示スキップ: {e}")


def _ensure_week_open(page):
    """「週表示」セクションが閉じていたら開く"""
    try:
        if page.locator("#week-info").count() == 0:
            page.get_by_role("button", name=re.compile("週表示")).first.click()
            page.wait_for_timeout(1500)
    except Exception:  # noqa: BLE001
        pass


def _parse_week_table(page, label, date_set, filters, results):
    table = page.locator("#week-info")
    if table.count() == 0:
        dump_debug(page, "shinagawa_no_weekinfo")
        return
    # 年月はページ内の「YYYY年M月」表記から取得
    body = page.inner_text("body")
    m = re.search(r"(\d{4})年\s*(\d{1,2})月", body)
    year = int(m.group(1)) if m else dt.date.today().year

    data = table.evaluate(
        """(t) => [...t.rows].map(r => [...r.cells].map(c => {
              const img = c.querySelector('img');
              return {text: c.textContent.trim(), alt: img ? img.alt : null};
           }))"""
    )
    if not data:
        return
    header = data[0]
    # 列 -> 日付 (「7月11日土曜」形式)
    col_dates = {}
    for i, cell in enumerate(header):
        dm = re.search(r"(\d{1,2})月(\d{1,2})日", cell["text"])
        if dm:
            mon, day = int(dm.group(1)), int(dm.group(2))
            y = year
            # 12月→1月をまたぐ場合の補正
            if m and mon < int(m.group(2)):
                y += 1
            col_dates[i] = dt.date(y, mon, day).isoformat()
    for row in data[1:]:
        if not row:
            continue
        slot = row[0]["text"]  # 例 "11:00～"
        if not start_hour_ok(slot, filters["start_hour_min"], filters["start_hour_max"]):
            continue
        for i, cell in enumerate(row):
            if i not in col_dates:
                continue
            alt = cell["alt"] or ""
            if "空き" in alt and "空きなし" not in alt:
                d = col_dates[i]
                if d in date_set:
                    results.add((label, d, slot))
    time.sleep(1)
