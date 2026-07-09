"""品川区 しせつよやく (cm9.eprs.jp)
天王洲公園・八潮北公園の野球場。
ホーム画面の検索フォームは折りたたみ式なので、フィールドIDを直接JSで操作する。
  #thismonth(いつ=1か月) #daystart(開始日) #days(期間)
  dayofweek: #saturday #sunday #holiday / timezone: #allday
  #bname(館) #iname(施設) #purpose(利用目的) #btn-go(検索)
結果ページの #week-info テーブル（時間帯×日付）を読む。img alt が「空き」なら空きコマ。
"""
import datetime as dt
import re
import time

from ..util import UA, SkipSite, dump_debug, start_hour_ok, today_jst

HOME = "https://www.cm9.eprs.jp/shinagawa/web/"
BOOKING_URL = HOME


def fetch(browser, cfg, filters, dates) -> set[tuple[str, str, str]]:
    date_set = {d.isoformat() for d in dates}
    results: set[tuple[str, str, str]] = set()
    room_re = re.compile(cfg.get("room_filter", "野球"))

    for venue in cfg.get("venues", []):
        ctx = browser.new_context(user_agent=UA, locale="ja-JP")
        page = ctx.new_page()
        try:
            print(f"[shinagawa] {venue} を検索")
            page.goto(HOME, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2500)

            head = (page.title() or "") + page.inner_text("body")[:500]
            if any(w in head for w in ("休止", "メンテナンス", "サービス時間外", "利用時間外")):
                raise SkipSite("品川区システムが停止時間帯")

            ok = page.evaluate(
                """([venue, purpose, dayISO]) => {
                  const log = [];
                  const q = (s) => document.querySelector(s);
                  // いつ: 開始日=今日, 期間=1か月
                  const ds = q('#daystart'); if (ds) { ds.value = dayISO; log.push('daystart'); }
                  const days = q('#days');
                  if (days) {
                    const opt = [...days.options].find(o => /1か月|1ヶ月/.test(o.textContent));
                    if (opt) { days.value = opt.value; days.dispatchEvent(new Event('change',{bubbles:true})); log.push('days=1month'); }
                  }
                  // 曜日: 土日祝 / 時間帯: 終日
                  for (const id of ['saturday','sunday','holiday','allday']) {
                    const c = q('#'+id);
                    if (c && !c.checked) { c.click(); log.push(id); }
                  }
                  // 館
                  const b = q('#bname');
                  if (!b) return {ok:false, log};
                  const bo = [...b.options].find(o => o.textContent.trim() === venue);
                  if (!bo) return {ok:false, log:[...log,'venue not found']};
                  b.value = bo.value;
                  b.dispatchEvent(new Event('change',{bubbles:true}));
                  log.push('bname');
                  // 利用目的
                  const p = q('#purpose');
                  if (p) {
                    const po = [...p.options].find(o => o.textContent.trim() === purpose);
                    if (po) { p.value = po.value; p.dispatchEvent(new Event('change',{bubbles:true})); log.push('purpose'); }
                  }
                  return {ok:true, log};
                }""",
                [venue, cfg.get("purpose", "野球"), today_jst().isoformat()],
            )
            print(f"[shinagawa] フォーム設定: {ok}")
            if not ok.get("ok"):
                dump_debug(page, f"shinagawa_form_{venue}")
                raise RuntimeError(f"検索フォーム設定失敗: {ok}")

            page.locator("#btn-go").click()
            page.wait_for_load_state("domcontentloaded")
            page.wait_for_timeout(3000)

            # 結果ページ: 施設(#iname相当)のドロップダウンを順に読む
            _read_venue_results(page, venue, room_re, date_set, filters, results)
        except Exception:
            dump_debug(page, f"shinagawa_error_{venue}")
            raise
        finally:
            ctx.close()
        time.sleep(2)
    return results


def _read_venue_results(page, venue, room_re, date_set, filters, results):
    _ensure_week_open(page)
    # 結果ページの 館/施設 ドロップダウン（selectが2つ並ぶ）
    selects = page.locator("select")
    room_sel = None
    for i in range(selects.count()):
        opts = selects.nth(i).locator("option").all_text_contents()
        if any(room_re.search(o) for o in opts):
            room_sel = selects.nth(i)
            room_opts = [o.strip() for o in opts if o.strip() and room_re.search(o)]
            break
    if room_sel is None:
        print(f"[shinagawa] 施設ドロップダウン無し: そのまま読む")
        _read_months(page, venue, date_set, filters, results)
        return
    for r in room_opts:
        room_sel.select_option(label=r)
        page.wait_for_timeout(2000)
        _read_months(page, f"{venue} {r}", date_set, filters, results)


def _read_months(page, label, date_set, filters, results):
    """今月と翌月の #week-info を読む"""
    _parse_week_table(page, label, date_set, filters, results)
    try:
        page.get_by_role("button", name=re.compile("翌月")).first.click()
        page.wait_for_timeout(2500)
        _parse_week_table(page, label, date_set, filters, results)
        page.get_by_role("button", name=re.compile("前月")).first.click()
        page.wait_for_timeout(2000)
    except Exception as e:  # noqa: BLE001
        print(f"[shinagawa] 翌月表示スキップ: {e}")


def _ensure_week_open(page):
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
    body = page.inner_text("body")
    m = re.search(r"(\d{4})年\s*(\d{1,2})月", body)
    year = int(m.group(1)) if m else dt.date.today().year

    data = table.first.evaluate(
        """(t) => [...t.rows].map(r => [...r.cells].map(c => {
              const img = c.querySelector('img');
              return {text: c.textContent.trim(), alt: img ? img.alt : null};
           }))"""
    )
    if not data:
        return
    header = data[0]
    col_dates = {}
    for i, cell in enumerate(header):
        dm = re.search(r"(\d{1,2})月(\d{1,2})日", cell["text"])
        if dm:
            mon, day = int(dm.group(1)), int(dm.group(2))
            y = year
            if m and mon < int(m.group(2)):
                y += 1
            col_dates[i] = dt.date(y, mon, day).isoformat()
    hits = 0
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
                    hits += 1
    print(f"[shinagawa] {label}: {len(col_dates)}日分読み取り, 空き{hits}件")
