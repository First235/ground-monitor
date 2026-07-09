"""大田区 うぐいすネット (yoyaku.city.ota.tokyo.jp) CGI型。実画面確認済み。
流れ: トップ(Welcome.cgi)
 → 申込の種類「施設の空き照会／予約申込」
 → 検索条件「カテゴリで検索」→ カテゴリ「大田スタジアム」→ 選択した条件で次へ
 → 施設選択(Login.cgi): 施設を全て選択する → 選択した施設で検索
 → 空き状況グリッド(ShisetsuMultiSelect.cgi): table.box_calendar
   行=時間帯(07:00 - 09:00 等) / 列=日付(7月9日+曜日) / セル=img alt
   alt「空き」を含めば空きコマ。「次の7日分」で週送り。
"""
import datetime as dt
import re

from ..util import UA, SkipSite, dump_debug, start_hour_ok, today_jst

HOME = "https://www.yoyaku.city.ota.tokyo.jp/eshisetsu/menu/Welcome.cgi"
BOOKING_URL = "https://www.yoyaku.city.ota.tokyo.jp/"


def fetch(browser, cfg, filters, dates) -> set[tuple[str, str, str]]:
    date_set = {d.isoformat() for d in dates}
    end_iso = max(date_set)
    results: set[tuple[str, str, str]] = set()
    ctx = browser.new_context(user_agent=UA, locale="ja-JP")
    page = ctx.new_page()
    try:
        page.goto(HOME, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)

        # 夜間などのシステム休止を検知（エラー扱いにしない）
        if "休止" in (page.title() or ""):
            raise SkipSite("うぐいすネットがシステム休止中")

        # 1. 申込の種類: 施設の空き照会／予約申込（ラジオをJSで確実に選択）
        _js_check_radio_by_label(page, "施設の空き照会／予約申込")
        print("[ota] 申込の種類 選択OK")

        # 2. 検索条件: カテゴリで検索
        _js_check_radio_by_label(page, "カテゴリで検索")
        print("[ota] カテゴリで検索 選択OK")

        # 3. カテゴリ: 大田スタジアム（config変更可）
        _js_check_radio_by_label(page, cfg.get("category", "大田スタジアム"))
        print("[ota] カテゴリ 選択OK")

        # 4. 選択した条件で次へ
        _click_any(page, "選択した条件で次へ")
        page.wait_for_url("**/Login.cgi**", timeout=30000)
        page.wait_for_timeout(3000)
        print(f"[ota] 施設選択ページ: {page.url}")

        # 5. 施設を全て選択する → 選択した施設で検索
        _click_any(page, "施設を全て選択")
        page.wait_for_timeout(1500)
        _click_any(page, "選択した施設で検索")
        page.wait_for_url("**/yoyaku/**", timeout=30000)
        page.wait_for_timeout(3000)
        print(f"[ota] 空き状況グリッド: {page.url}")

        # 6. 週送りしながらグリッドを読む（7日表示 × 最大10週 ≒ 対象期間をカバー）
        for week in range(10):
            max_date = _parse_grid(page, cfg, filters, date_set, results)
            print(f"[ota] week{week + 1}: 最終列 {max_date}, 累計空き {len(results)}件")
            if max_date and max_date >= end_iso:
                break
            if not _next_week(page):
                print("[ota] 次の7日分ボタンが押せないため終了")
                break
        return results
    except SkipSite:
        raise
    except Exception:
        dump_debug(page, "ota_error")
        raise
    finally:
        ctx.close()


def _click_any(page, text):
    """button でも a でも input でも、テキスト一致でクリック"""
    btn = page.get_by_role("button", name=re.compile(text))
    if btn.count() > 0:
        btn.first.click()
        return
    link = page.get_by_role("link", name=re.compile(text))
    if link.count() > 0:
        link.first.click()
        return
    page.get_by_text(re.compile(text)).first.click()


def _js_check_radio_by_label(page, label_text):
    """ボタン風ラジオ（label内テキスト一致）をJSでクリックして checked を確認"""
    ok = page.evaluate(
        """(txt) => {
          const labels = [...document.querySelectorAll('label')]
            .filter(l => l.textContent.replace(/\\s+/g, '').includes(txt.replace(/\\s+/g, '')));
          for (const l of labels) {
            const input = l.querySelector('input[type=radio]') ||
              (l.htmlFor ? document.getElementById(l.htmlFor) : null);
            if (input) { input.click(); return input.checked; }
            l.click();
            return true;
          }
          // labelが無い場合: テキストを持つ要素をクリック
          const els = [...document.querySelectorAll('button, span, div')]
            .filter(e => e.children.length === 0 && e.textContent.trim() === txt);
          if (els.length) { els[0].click(); return true; }
          return false;
        }""",
        label_text,
    )
    if not ok:
        dump_debug(page, f"ota_radio_{label_text[:8]}")
        raise RuntimeError(f"選択肢が見つからない: {label_text}")
    page.wait_for_timeout(800)


def _parse_grid(page, cfg, filters, date_set, results):
    """table.box_calendar を解析。戻り値は表中の最終日付(ISO)"""
    data = page.evaluate(
        """() => {
          const t = document.querySelector('table.box_calendar');
          if (!t) return null;
          return [...t.rows].map(r => [...r.cells].map(c => {
            const img = c.querySelector('img');
            return {text: c.textContent.trim().replace(/\\s+/g, ' '),
                    alt: img ? img.alt : null};
          }));
        }"""
    )
    if not data:
        dump_debug(page, "ota_no_grid")
        return None

    today = today_jst()
    header = data[0]
    col_dates = {}
    for i, cell in enumerate(header):
        dm = re.search(r"(\d{1,2})月(\d{1,2})日", cell["text"])
        if dm:
            mon, day = int(dm.group(1)), int(dm.group(2))
            year = today.year + (1 if mon < today.month else 0)
            col_dates[i] = dt.date(year, mon, day).isoformat()
    if not col_dates:
        dump_debug(page, "ota_no_dates")
        return None

    fac = cfg.get("category", "大田スタジアム")
    for row in data[1:]:
        if not row:
            continue
        slot = row[0]["text"]  # 例 "07:00 - 09:00"
        if not re.search(r"\d{1,2}:\d{2}", slot):
            continue
        if not start_hour_ok(slot, filters["start_hour_min"], filters["start_hour_max"]):
            continue
        for i, cell in enumerate(row):
            if i not in col_dates:
                continue
            alt = cell["alt"] or ""
            # 空きセルの alt は「空いています」(icn_scche_ok.png)
            if "空い" in alt or "空き" in alt:
                d = col_dates[i]
                if d in date_set:
                    results.add((fac, d, slot))
    return max(col_dates.values())


def _next_week(page) -> bool:
    """「次の7日分」は <a class="link next">"""
    try:
        clicked = page.evaluate(
            """() => {
              const a = document.querySelector('a.link.next');
              if (!a) return false;
              a.click();
              return true;
            }"""
        )
        if not clicked:
            return False
        page.wait_for_timeout(3000)
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[ota] 週送り失敗: {e}")
        return False
