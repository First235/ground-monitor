"""墨田区・杉並区 共通（「公共施設予約システム」/user/Home 型）
流れ: Home → 施設種類ボタン → 施設選択(チェック) → 次へ進む
→ 施設別空き状況(1ヶ月表示・日単位の○△×) → 空きセル選択 → 次へ進む
→ 時間帯別空き状況（「10時から12時まで」等のコマ）。
当月と翌月の2パスを、それぞれ新しいセッションで実行する。
"""
import datetime as dt
import re

from ..util import UA, SkipSite, dump_debug, start_hour_ok, today_jst

STATUS_AVAILABLE = ("available", "some")  # ラベルclass: 空き/一部空き


def fetch(browser, cfg, filters, dates) -> set[tuple[str, str, str]]:
    date_set = {d.isoformat() for d in dates}
    results: set[tuple[str, str, str]] = set()

    t = today_jst()
    next_month = dt.date(t.year + (1 if t.month == 12 else 0),
                         1 if t.month == 12 else t.month + 1, 1)
    for start in (t, next_month):
        _run_pass(browser, cfg, filters, date_set, start, results)
    return results


def _run_pass(browser, cfg, filters, date_set, start_date, results):
    ctx = browser.new_context(user_agent=UA, locale="ja-JP")
    page = ctx.new_page()
    name = cfg.get("name", "")
    try:
        page.goto(cfg["base_url"] + "/user/Home",
                  wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        head = (page.title() or "") + page.inner_text("body")[:500]
        if any(w in head for w in ("休止", "メンテナンス", "サービス時間外", "利用時間外")):
            raise SkipSite(f"{name}システムが停止時間帯")
        print(f"[{name}] Home 表示OK ({start_date}〜)")

        # 施設種類から探す → カテゴリボタン（1回で遷移しないことがあるためリトライ）
        for attempt in range(3):
            try:
                page.get_by_role("button", name=cfg["category_button"]).first.click()
                page.wait_for_url("**/AvailabilityCheckApplySelectFacility", timeout=8000)
                break
            except Exception:  # noqa: BLE001
                print(f"[{name}] カテゴリボタン再クリック ({attempt + 1})")
                page.wait_for_timeout(1500)
        else:
            raise RuntimeError("施設選択画面に遷移できない")
        page.wait_for_timeout(2000)
        print(f"[{name}] 施設選択 表示OK")

        # 施設にチェック（クリック後に checked を検証）
        for fac in cfg["facility_checkboxes"]:
            checked = page.evaluate(
                """(fac) => {
                  const labels = [...document.querySelectorAll('label')]
                    .filter(l => l.textContent.includes(fac));
                  for (const l of labels) {
                    const input = l.querySelector('input[type=checkbox]') ||
                      (l.htmlFor ? document.getElementById(l.htmlFor) : null) ||
                      l.closest('td,div')?.querySelector('input[type=checkbox]');
                    if (input) { if (!input.checked) input.click(); return input.checked; }
                    l.click();
                    return true;
                  }
                  return false;
                }""",
                fac,
            )
            print(f"[{name}] {fac} チェック: {checked}")
            if not checked:
                dump_debug(page, f"userhome_{name}_facility")
                raise RuntimeError(f"施設が見つからない: {fac}")
            page.wait_for_timeout(400)

        page.get_by_role("button", name="次へ進む").first.click()
        page.wait_for_url("**/AvailabilityCheckApplySelectDays", timeout=30000)
        page.wait_for_timeout(2500)
        print(f"[{name}] 施設別空き状況 表示OK")

        # 表示期間: 開始日と「1ヶ月」を設定して表示
        page.evaluate(
            """(dayISO) => {
              const d = document.querySelector('input[type=date]');
              if (d) {
                d.value = dayISO;
                d.dispatchEvent(new Event('input', {bubbles: true}));
                d.dispatchEvent(new Event('change', {bubbles: true}));
              }
              // 「1ヶ月」ラジオ
              const radios = [...document.querySelectorAll('input[type=radio]')];
              for (const r of radios) {
                const l = r.closest('label') ||
                  (r.id ? document.querySelector(`label[for="${r.id}"]`) : null);
                const txt = (l ? l.textContent : '') + (r.parentElement ? r.parentElement.textContent : '');
                if (/1ヶ月|1か月/.test(txt)) { if (!r.checked) r.click(); return; }
              }
            }""",
            start_date.isoformat(),
        )
        page.wait_for_timeout(500)
        page.get_by_role("button", name="表示").first.click()
        page.wait_for_timeout(4000)
        print(f"[{name}] 1ヶ月表示に切替OK")

        # 日単位グリッドを解析して空き(○/△)セルを選択
        row_re = re.compile(cfg.get("row_filter", "."))
        n_selected = page.evaluate(
            """(rowFilter) => {
              const re = new RegExp(rowFilter);
              const table = [...document.querySelectorAll('table')]
                .find(t => t.querySelector('td.startdate'));
              if (!table) return -1;
              // ヘッダの日付列
              const ths = [...table.querySelectorAll('thead th')].map(h => h.textContent.trim());
              let count = 0;
              for (const r of table.querySelectorAll('tbody tr')) {
                const nameCell = r.querySelector('td.startdate');
                if (!nameCell || !re.test(nameCell.textContent)) continue;
                for (const c of [...r.cells]) {
                  const l = c.querySelector('label');
                  if (!l) continue;
                  if (/(^| )(available|some)( |$)/.test(l.className)) {
                    l.click();
                    count++;
                  }
                }
              }
              return count;
            }""",
            cfg.get("row_filter", "."),
        )
        if n_selected == -1:
            dump_debug(page, f"userhome_{name}_no_grid")
            return
        if n_selected == 0:
            return  # 空きなし

        page.get_by_role("button", name="次へ進む").first.click()
        page.wait_for_url("**/AvailabilityCheckApplySelectTime", timeout=30000)
        page.wait_for_timeout(3000)

        _parse_time_page(page, name, row_re, filters, date_set, results)
    except Exception:
        dump_debug(page, f"userhome_{name}_error")
        raise
    finally:
        ctx.close()


def _parse_time_page(page, name, row_re, filters, date_set, results):
    """時間帯別空き状況: 日付見出しごとに、選択可能なコマ（label）を拾う。
    「10時から12時まで」のようなラベルが押せる状態＝空きあり。"""
    data = page.evaluate(
        """() => {
          const out = [];
          // 日付見出し（例 2026年 7月13日 (月)）を持つブロックを探す
          const all = [...document.querySelectorAll('*')];
          let currentDate = null, currentFac = null;
          const walk = (node) => {
            for (const el of node.children || []) {
              const own = [...el.childNodes]
                .filter(n => n.nodeType === 3)
                .map(n => n.textContent).join(' ').trim();
              const dm = own.match(/(\\d{4})\\s*年\\s*(\\d{1,2})\\s*月\\s*(\\d{1,2})\\s*日/);
              if (dm) currentDate = `${dm[1]}-${String(dm[2]).padStart(2,'0')}-${String(dm[3]).padStart(2,'0')}`;
              if (el.tagName === 'LABEL' || el.tagName === 'BUTTON' || (el.tagName === 'A' && el.textContent.includes('時から'))) {
                const t = el.textContent.trim();
                const tm = t.match(/(\\d{1,2})時から(\\d{1,2})時まで/);
                if (tm && currentDate) {
                  // 近くの施設名（tr内の最初のセル等）を探す
                  const row = el.closest('tr');
                  let fac = '';
                  if (row && row.cells.length) fac = row.cells[0].textContent.trim();
                  if (!fac) {
                    const h = el.closest('table');
                    if (h) {
                      const cap = h.closest('div');
                      if (cap) {
                        const hd = cap.querySelector('h1,h2,h3,h4,.facility-name');
                        if (hd) fac = hd.textContent.trim();
                      }
                    }
                  }
                  const disabled = el.classList.contains('disabled') ||
                                   el.querySelector('input') && el.querySelector('input').disabled;
                  out.push({date: currentDate, fac, slot: `${tm[1]}時から${tm[2]}時まで`,
                            disabled: !!disabled, text: t});
                }
              }
              walk(el);
            }
          };
          walk(document.body);
          return out;
        }"""
    )
    if not data:
        dump_debug(page, f"userhome_{name}_no_slots")
        return
    for item in data:
        if item["disabled"]:
            continue
        # 「空きなし」等の文字を含むものは除外
        if "空きなし" in item["text"] or "問合せ" in item["text"]:
            continue
        d = item["date"]
        slot = item["slot"]
        fac = re.sub(r"≪.*?≫", "", item["fac"] or name).strip() or name
        if d not in date_set:
            continue
        if not start_hour_ok(slot, filters["start_hour_min"], filters["start_hour_max"]):
            continue
        results.add((fac, d, slot))
