"""中央区 (www.11489.jp/Chuo) ASP.NET WebForms 型。
※トップページは frameset なので、name="center" のフレーム内を操作する。
流れ: トップ → 公共施設予約メニュー → 空き照会・予約の申込 → 区立運動場等
→ 日時選択(1ヶ月・土日祝) → 施設別空き状況(○△×) → セル選択 → 時間帯別空き状況。
時間帯別の解析に失敗した場合は日単位（○/△）で通知する。
"""
import datetime as dt
import re

from ..util import UA, SkipSite, dump_debug, start_hour_ok

HOME = "https://www.11489.jp/Chuo/web/"
BOOKING_URL = HOME


def fetch(browser, cfg, filters, dates) -> set[tuple[str, str, str]]:
    date_set = {d.isoformat() for d in dates}
    results: set[tuple[str, str, str]] = set()
    ctx = browser.new_context(user_agent=UA, locale="ja-JP")
    page = ctx.new_page()
    fr = page
    try:
        page.goto(HOME, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)
        fr = _get_frame(page)
        print(f"[chuo] frame url: {fr.url}")

        head = fr.evaluate("() => document.body ? document.body.innerText.slice(0, 500) : ''")
        if any(w in head for w in ("休止", "メンテナンス", "サービス時間外", "利用時間外")):
            raise SkipSite("中央区システムが停止時間帯")

        _click(page, fr, "公共施設予約メニュー")
        fr = _get_frame(page)
        _click(page, fr, re.compile("空き照会・予約の申込"))
        fr = _get_frame(page)

        # 施設検索: 「区立運動場等」を選んで次へ
        fr.get_by_text("区立運動場等", exact=False).first.click()
        page.wait_for_timeout(1500)
        _click(page, fr, re.compile("次へ"))
        fr = _get_frame(page)
        print(f"[chuo] 日時選択へ: {fr.url}")

        # 日時選択: 1ヶ月・土日祝（各クリックでポストバック）
        for name in ["1ヶ月", "土", "日", "祝"]:
            fr = _get_frame(page)
            _click(page, fr, name, exact=True)
        fr = _get_frame(page)
        _click(page, fr, re.compile("次へ"))
        fr = _get_frame(page)
        print(f"[chuo] 施設別空き状況: {fr.url}")

        # 施設別空き状況（今月）→ 次の期間（翌月）
        for period in range(2):
            fr = _get_frame(page)
            month_hits = _parse_month_grid(fr, cfg["facilities"], date_set)
            print(f"[chuo] 期間{period + 1}: ○△セル {len(month_hits)}件")
            if month_hits:
                slot_results = _drill_time_slots(page, fr, month_hits, filters)
                if slot_results is None:
                    for fac, d, status in month_hits:
                        results.add((fac, d, f"終日({status})※要時間確認"))
                else:
                    results.update(slot_results)
            if period == 0:
                try:
                    fr = _get_frame(page)
                    _click(page, fr, re.compile("次の期間"))
                except Exception:  # noqa: BLE001
                    break
        return results
    except Exception:
        dump_debug(fr, "chuo_error")
        raise
    finally:
        ctx.close()


def _get_frame(page):
    """frameset の中身（アプリ本体）のフレームを取得"""
    for f in page.frames:
        if f is not page.main_frame and ("Wg_" in f.url or "StartPage" in f.url or f.name == "center"):
            return f
    # フレームが無い場合はページ本体を返す
    return page.main_frame


def _click(page, fr, name, exact=False):
    fr.get_by_role("button", name=name, exact=exact).first.click()
    page.wait_for_timeout(2500)


def _parse_month_grid(fr, facilities, date_set):
    """施設別空き状況の表から (施設, 日付, ○|△) を抽出"""
    body = fr.evaluate("() => document.body.innerText")
    ym = re.search(r"(\d{4})年(\d{1,2})月", body)
    if not ym:
        dump_debug(fr, "chuo_no_month")
        return []
    year, month = int(ym.group(1)), int(ym.group(2))

    data = fr.evaluate(
        """() => {
          const tables = [...document.querySelectorAll('table')];
          const t = tables.find(t => /定員/.test(t.textContent) && /[○△×－]/.test(t.textContent));
          if (!t) return null;
          return [...t.rows].map(r => [...r.cells].map(c => c.textContent.trim()));
        }"""
    )
    if not data:
        dump_debug(fr, "chuo_no_grid")
        return []

    header, hidx = None, -1
    for i, row in enumerate(data):
        if sum(1 for c in row if re.fullmatch(r"\d{1,2}\s*[月火水木金土日祝]", c)) >= 3:
            header, hidx = row, i
            break
    if header is None:
        dump_debug(fr, "chuo_no_header")
        return []

    col_dates = {}
    prev_day = 0
    m, y = month, year
    for i, c in enumerate(header):
        dm = re.fullmatch(r"(\d{1,2})\s*[月火水木金土日祝]", c)
        if dm:
            day = int(dm.group(1))
            if day < prev_day:  # 月替わり（例 26 → 1）
                m += 1
                if m > 12:
                    m, y = 1, y + 1
            prev_day = day
            col_dates[i] = dt.date(y, m, day).isoformat()

    hits = []
    for row in data[hidx + 1:]:
        if not row:
            continue
        fac = row[0]
        if fac not in facilities:
            continue
        statuses = [c for c in row[1:] if c in ("○", "△", "×", "－", "休", "＊", "選択")]
        date_cols = sorted(col_dates.keys())
        for j, st in enumerate(statuses):
            if j >= len(date_cols):
                break
            d = col_dates[date_cols[j]]
            if st in ("○", "△") and d in date_set:
                hits.append((fac, d, st))
    return hits


def _drill_time_slots(page, fr, month_hits, filters):
    """○/△セルを選択して時間帯別空き状況へ進み、時間帯を取得。
    失敗したら None（呼び出し側で日単位にフォールバック）"""
    try:
        selected = 0
        for fac in sorted({f for f, _, _ in month_hits}):
            row = fr.locator("tr", has_text=fac).last
            links = row.get_by_role("link")
            for k in range(links.count()):
                t = links.nth(k).inner_text().strip()
                if t in ("○", "△"):
                    links.nth(k).click()
                    page.wait_for_timeout(500)
                    selected += 1
        print(f"[chuo] セル選択 {selected}件")
        if selected == 0:
            return None
        _click(page, fr, re.compile("次へ"))
        fr2 = _get_frame(page)
        slots = _parse_time_page(fr2, filters)
        print(f"[chuo] 時間帯別: {len(slots)}件")
        _click(page, fr2, re.compile("戻る"))
        return slots
    except Exception as e:  # noqa: BLE001
        print(f"[chuo] 時間帯別の取得に失敗: {e}")
        dump_debug(fr, "chuo_timeslot_error")
        return None


def _parse_time_page(fr, filters):
    results = set()
    tables = fr.evaluate(
        """() => [...document.querySelectorAll('table')].map(t => ({
             text: t.textContent,
             rows: [...t.rows].map(r => [...r.cells].map(c => c.textContent.trim()))
           }))"""
    )
    for t in tables:
        if "時" not in t["text"] or not re.search(r"[○△]", t["text"]):
            continue
        rows = t["rows"]
        if not rows:
            continue
        header = rows[0]
        time_cols = {}
        for i, c in enumerate(header):
            c2 = _z2h(c)
            if re.search(r"\d{1,2}\s*[:時]", c2) and ("～" in c2 or "〜" in c2 or "-" in c2):
                time_cols[i] = c2
        if not time_cols:
            continue
        for row in rows[1:]:
            joined = _z2h(" ".join(row))
            dm = re.search(r"(\d{4})[年/](\d{1,2})[月/](\d{1,2})", joined)
            if not dm:
                continue
            d = dt.date(int(dm.group(1)), int(dm.group(2)), int(dm.group(3))).isoformat()
            fac = row[0].strip() if row else ""
            for i, cell in enumerate(row):
                if i in time_cols and cell.strip() in ("○", "△"):
                    label = time_cols[i]
                    if start_hour_ok(label, filters["start_hour_min"], filters["start_hour_max"]):
                        results.add((fac or "中央区施設", d, label))
    return results


def _z2h(s: str) -> str:
    return s.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
