#!/usr/bin/env python3
"""
GitHub Actions용 포트폴리오 모니터링
====================================
- 단일 실행 (Actions cron이 반복 담당)
- 환경변수에서 텔레그램 설정 읽기
- status.json 출력 → GitHub Pages 대시보드 연동
"""

import requests
from bs4 import BeautifulSoup
import json
import os
import sys
from datetime import datetime
from pathlib import Path

# ============================================================
# 설정
# ============================================================

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36"
}

# 종목 코드
STOCKS = {
    "samsung": {"code": "005930", "name": "삼성전자"},
    "hana":    {"code": "086790", "name": "하나금융지주"},
}

# 규칙 임계값
RULES = {
    "samsung_warning":  183000,
    "samsung_critical": 170000,
    "hana_critical":    107000,
    "wti_warning":      75,
    "wti_critical":     80,
    "krw_warning":      1480,
    "krw_critical":     1500,
}

# ============================================================
# 시세 조회 — 네이버 금융
# ============================================================

def get_naver_price(code: str) -> dict | None:
    url = f"https://finance.naver.com/item/main.naver?code={code}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")
        price_tag = soup.select_one("p.no_today span.blind")
        if not price_tag:
            return None
        price = int(price_tag.text.replace(",", ""))
        prev_tag = soup.select_one("td.first span.blind")
        prev_close = int(prev_tag.text.replace(",", "")) if prev_tag else None
        change_pct = None
        if prev_close and prev_close > 0:
            change_pct = round((price - prev_close) / prev_close * 100, 2)
        return {"price": price, "prev_close": prev_close, "change_pct": change_pct}
    except Exception as e:
        print(f"[ERROR] 네이버 시세 조회 실패 ({code}): {e}")
        return None


def get_kospi_index() -> dict | None:
    url = "https://finance.naver.com/sise/sise_index.naver?code=KOSPI"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")
        now_tag = soup.select_one("#now_value")
        if not now_tag:
            return None
        value = float(now_tag.text.replace(",", ""))
        change_rate_tag = soup.select_one("#change_rate_01")
        change_pct = None
        if change_rate_tag:
            rate_text = change_rate_tag.text.strip().replace("%", "")
            try:
                change_pct = float(rate_text)
            except ValueError:
                pass
        blind_tag = soup.select_one("#quotient")
        if blind_tag and "하락" in blind_tag.text:
            if change_pct and change_pct > 0:
                change_pct = -change_pct
        return {"value": value, "change_pct": change_pct}
    except Exception as e:
        print(f"[ERROR] 코스피 조회 실패: {e}")
        return None


# ============================================================
# 시세 조회 — 네이버 환율 + 유가 (Yahoo 대신 네이버 사용)
# ============================================================

def _parse_naver_market_digits(soup) -> float | None:
    """네이버 시장지표 페이지의 숫자 파싱 (각 자릿수가 개별 span)
    span 클래스: no0~no9=숫자, shim=쉼표, jum=소수점"""
    container = soup.select_one("p.no_today em em")  # 환율: em>em
    if not container:
        container = soup.select_one("p.no_today em")  # WTI: em
    if not container:
        return None
    digits = ""
    for span in container.find_all("span", recursive=False):
        cls = span.get("class", [])
        cls_str = cls[0] if cls else ""
        if cls_str.startswith("no"):
            digits += cls_str[2:]  # "no7" → "7"
        elif cls_str == "shim":
            pass  # 쉼표 무시
        elif cls_str == "jum":
            digits += "."
        elif cls_str in ("txt_won", "txt_unit"):
            break
    try:
        return float(digits)
    except ValueError:
        return None


def get_usdkrw_naver() -> dict | None:
    """네이버 금융 환율 페이지에서 USD/KRW 조회"""
    url = "https://finance.naver.com/marketindex/exchangeDetail.naver?marketindexCd=FX_USDKRW"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")
        rate = _parse_naver_market_digits(soup)
        if rate:
            return {"rate": rate}
        return None
    except Exception as e:
        print(f"[ERROR] 네이버 환율 조회 실패: {e}")
        return None


def get_wti_naver() -> dict | None:
    """네이버 금융 원자재 페이지에서 WTI 조회"""
    url = "https://finance.naver.com/marketindex/materialDetail.naver?marketindexCd=OIL_CL"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")
        price = _parse_naver_market_digits(soup)
        if price:
            return {"price": price}
        return None
    except Exception as e:
        print(f"[ERROR] 네이버 WTI 조회 실패: {e}")
        return None


def get_wti_yahoo() -> dict | None:
    """Yahoo Finance fallback for WTI"""
    try:
        import yfinance as yf
        ticker = yf.Ticker("CL=F")
        data = ticker.fast_info
        price = data.get("lastPrice") or data.get("last_price")
        if price:
            return {"price": round(price, 2)}
    except Exception as e:
        print(f"[ERROR] Yahoo WTI 조회 실패: {e}")
    return None


def get_usdkrw_yahoo() -> dict | None:
    """Yahoo Finance fallback for USD/KRW"""
    try:
        import yfinance as yf
        ticker = yf.Ticker("KRW=X")
        data = ticker.fast_info
        price = data.get("lastPrice") or data.get("last_price")
        if price:
            return {"rate": round(price, 2)}
    except Exception as e:
        print(f"[ERROR] Yahoo 환율 조회 실패: {e}")
    return None


# ============================================================
# 텔레그램 알림
# ============================================================

def send_telegram(message: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[SKIP] 텔레그램 미설정")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            print("[OK] 텔레그램 전송 완료")
        else:
            print(f"[ERROR] 텔레그램 전송 실패: {resp.text}")
    except Exception as e:
        print(f"[ERROR] 텔레그램 전송 에러: {e}")


# ============================================================
# 규칙 엔진
# ============================================================

def check_rules(data: dict) -> list[dict]:
    alerts = []

    samsung = data.get("samsung")
    hana = data.get("hana")
    kospi = data.get("kospi")
    wti = data.get("wti")
    usdkrw = data.get("usdkrw")

    # 삼성전자
    if samsung and samsung.get("price"):
        p = samsung["price"]
        if p <= RULES["samsung_critical"]:
            alerts.append({
                "level": "critical", "target": "삼성전자",
                "msg": f"<b>[긴급] 삼성전자 {p:,}원</b> — 추가 매도 검토\n15~20주 추가 매도 검토"
            })
        elif p <= RULES["samsung_warning"]:
            alerts.append({
                "level": "warning", "target": "삼성전자",
                "msg": f"<b>[경고] 삼성전자 마지노선 접근</b>\n현재가: {p:,}원 | 180,000원까지 {p-180000:,}원"
            })

    # 하나금융
    if hana and hana.get("price"):
        p = hana["price"]
        if p <= RULES["hana_critical"]:
            alerts.append({
                "level": "critical", "target": "하나금융",
                "msg": f"<b>[긴급] 하나금융 {p:,}원 이탈</b> — 92주 전량 매도"
            })

    # 하나금융 방어축 점검
    if (hana and hana.get("change_pct") is not None
            and kospi and kospi.get("change_pct") is not None):
        h_chg, k_chg = hana["change_pct"], kospi["change_pct"]
        if h_chg < 0 and k_chg < 0 and h_chg < k_chg:
            excess = round(h_chg - k_chg, 2)
            alerts.append({
                "level": "warning", "target": "하나금융",
                "msg": f"<b>[경고] 하나금융 방어축 점검</b>\n하나: {h_chg}% | 코스피: {k_chg}% | 초과하락: {excess}%p"
            })

    # WTI
    if wti and wti.get("price"):
        wp = wti["price"]
        if wp >= RULES["wti_critical"]:
            alerts.append({
                "level": "critical", "target": "WTI",
                "msg": f"<b>[긴급] WTI ${wp}</b> — $80 돌파"
            })
        elif wp >= RULES["wti_warning"]:
            alerts.append({
                "level": "warning", "target": "WTI",
                "msg": f"<b>[경고] WTI ${wp}</b> — $75 경고선 돌파"
            })

    # 환율
    if usdkrw and usdkrw.get("rate"):
        r = usdkrw["rate"]
        if r >= RULES["krw_critical"]:
            alerts.append({
                "level": "critical", "target": "환율",
                "msg": f"<b>[긴급] 원/달러 {r:,.0f}원</b> — 1,500원 돌파"
            })
        elif r >= RULES["krw_warning"]:
            alerts.append({
                "level": "warning", "target": "환율",
                "msg": f"<b>[경고] 원/달러 {r:,.0f}원</b> — 1,480 경고선 돌파"
            })

    # 복합 트리거
    if (wti and wti.get("price") and wti["price"] >= RULES["wti_critical"]
            and usdkrw and usdkrw.get("rate") and usdkrw["rate"] >= RULES["krw_critical"]):
        alerts.append({
            "level": "critical", "target": "복합트리거",
            "msg": (f"<b>[최긴급] 실행 트리거 발동!</b>\n"
                    f"WTI: ${wti['price']} + 환율: {usdkrw['rate']:,.0f}원\n"
                    f"현금 25%+ 확대, 삼성 10~15주 추가 매도")
        })

    return alerts


# ============================================================
# 상태 JSON 생성
# ============================================================

def build_status(data: dict, alerts: list[dict]) -> dict:
    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    kst = (datetime.utcnow() + __import__("datetime").timedelta(hours=9)).strftime("%Y-%m-%d %H:%M KST")

    status = {
        "updated_at": now,
        "updated_kst": kst,
        "market": {},
        "alerts": [],
        "rules": RULES,
    }

    if data.get("samsung"):
        s = data["samsung"]
        status["market"]["samsung"] = {
            "name": "삼성전자", "price": s["price"],
            "change_pct": s.get("change_pct"),
            "to_warning": s["price"] - RULES["samsung_warning"],
            "to_critical": s["price"] - RULES["samsung_critical"],
        }

    if data.get("hana"):
        h = data["hana"]
        status["market"]["hana"] = {
            "name": "하나금융지주", "price": h["price"],
            "change_pct": h.get("change_pct"),
            "to_critical": h["price"] - RULES["hana_critical"],
        }

    if data.get("kospi"):
        k = data["kospi"]
        status["market"]["kospi"] = {
            "name": "코스피", "value": k["value"],
            "change_pct": k.get("change_pct"),
        }

    if data.get("wti"):
        w = data["wti"]
        status["market"]["wti"] = {
            "name": "WTI 원유", "price": w["price"],
            "to_warning": round(RULES["wti_warning"] - w["price"], 2),
            "to_critical": round(RULES["wti_critical"] - w["price"], 2),
        }

    if data.get("usdkrw"):
        u = data["usdkrw"]
        status["market"]["usdkrw"] = {
            "name": "원/달러", "rate": u["rate"],
            "to_warning": round(RULES["krw_warning"] - u["rate"], 1),
            "to_critical": round(RULES["krw_critical"] - u["rate"], 1),
        }

    for a in alerts:
        status["alerts"].append({
            "level": a["level"], "target": a["target"],
            "msg": a["msg"].replace("<b>", "").replace("</b>", "")
        })

    return status


# ============================================================
# 대시보드 HTML 생성
# ============================================================

def generate_dashboard(status: dict) -> str:
    market = status.get("market", {})

    def fmt_price(v, unit="원"):
        if v is None:
            return "N/A"
        if isinstance(v, float):
            return f"{v:,.1f}{unit}"
        return f"{v:,}{unit}"

    def fmt_pct(v):
        if v is None:
            return ""
        sign = "+" if v > 0 else ""
        return f"{sign}{v}%"

    def pct_class(v):
        if v is None:
            return ""
        return "up" if v > 0 else "down" if v < 0 else ""

    def dist_bar(current, target, direction="above"):
        """거리 바 퍼센트 계산 (0~100)"""
        if current is None or target is None:
            return 50
        if direction == "above":
            # 현재가가 target 위에 있어야 안전 (종목)
            diff = current - target
            safe_range = target * 0.15  # 15% 범위
        else:
            # 현재가가 target 아래에 있어야 안전 (WTI, 환율)
            diff = target - current
            safe_range = target * 0.10
        if safe_range == 0:
            return 50
        pct = max(0, min(100, diff / safe_range * 100))
        return round(pct)

    # 카드 생성
    cards_html = ""

    # 삼성전자
    if "samsung" in market:
        s = market["samsung"]
        pct = dist_bar(s["price"], RULES["samsung_critical"], "above")
        cards_html += f"""
        <div class="card">
          <div class="card-header">
            <span class="card-title">삼성전자</span>
            <span class="badge {pct_class(s.get('change_pct'))}">{fmt_pct(s.get('change_pct'))}</span>
          </div>
          <div class="price">{s['price']:,}원</div>
          <div class="distances">
            <div class="dist-item">경고선 183K까지 <b>{s['to_warning']:,}원</b></div>
            <div class="dist-item">마지노 170K까지 <b>{s['to_critical']:,}원</b></div>
          </div>
          <div class="bar-wrap"><div class="bar bar-safe" style="width:{pct}%"></div></div>
        </div>"""

    # 하나금융
    if "hana" in market:
        h = market["hana"]
        pct = dist_bar(h["price"], RULES["hana_critical"], "above")
        cards_html += f"""
        <div class="card">
          <div class="card-header">
            <span class="card-title">하나금융지주</span>
            <span class="badge {pct_class(h.get('change_pct'))}">{fmt_pct(h.get('change_pct'))}</span>
          </div>
          <div class="price">{h['price']:,}원</div>
          <div class="distances">
            <div class="dist-item">마지노 107K까지 <b>{h['to_critical']:,}원</b></div>
          </div>
          <div class="bar-wrap"><div class="bar bar-safe" style="width:{pct}%"></div></div>
        </div>"""

    # 코스피
    if "kospi" in market:
        k = market["kospi"]
        cards_html += f"""
        <div class="card">
          <div class="card-header">
            <span class="card-title">코스피</span>
            <span class="badge {pct_class(k.get('change_pct'))}">{fmt_pct(k.get('change_pct'))}</span>
          </div>
          <div class="price">{k['value']:,.2f}</div>
        </div>"""

    # WTI
    if "wti" in market:
        w = market["wti"]
        pct = dist_bar(w["price"], RULES["wti_critical"], "below")
        cards_html += f"""
        <div class="card">
          <div class="card-header">
            <span class="card-title">WTI 원유</span>
          </div>
          <div class="price">${w['price']}</div>
          <div class="distances">
            <div class="dist-item">경고선 $75까지 <b>${w['to_warning']}</b></div>
            <div class="dist-item">트리거 $80까지 <b>${w['to_critical']}</b></div>
          </div>
          <div class="bar-wrap"><div class="bar bar-safe" style="width:{pct}%"></div></div>
        </div>"""

    # 환율
    if "usdkrw" in market:
        u = market["usdkrw"]
        pct = dist_bar(u["rate"], RULES["krw_critical"], "below")
        cards_html += f"""
        <div class="card">
          <div class="card-header">
            <span class="card-title">원/달러</span>
          </div>
          <div class="price">{u['rate']:,.1f}원</div>
          <div class="distances">
            <div class="dist-item">경고선 1,480까지 <b>{u['to_warning']:,.1f}원</b></div>
            <div class="dist-item">트리거 1,500까지 <b>{u['to_critical']:,.1f}원</b></div>
          </div>
          <div class="bar-wrap"><div class="bar bar-safe" style="width:{pct}%"></div></div>
        </div>"""

    # 알림 섹션
    alerts_html = ""
    if status.get("alerts"):
        for a in status["alerts"]:
            cls = "alert-critical" if a["level"] == "critical" else "alert-warning"
            alerts_html += f'<div class="alert {cls}">{a["msg"]}</div>\n'
    else:
        alerts_html = '<div class="alert alert-ok">현재 발동된 알림 없음</div>'

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no">
<title>Portfolio Monitor</title>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #0d1117; color: #e6edf3; padding: 12px;
    max-width: 480px; margin: 0 auto;
  }}
  header {{
    text-align: center; padding: 16px 0 8px;
    border-bottom: 1px solid #30363d; margin-bottom: 12px;
  }}
  header h1 {{ font-size: 18px; font-weight: 600; }}
  header .updated {{ font-size: 12px; color: #8b949e; margin-top: 4px; }}
  .card {{
    background: #161b22; border: 1px solid #30363d; border-radius: 10px;
    padding: 14px; margin-bottom: 10px;
  }}
  .card-header {{ display: flex; justify-content: space-between; align-items: center; }}
  .card-title {{ font-size: 14px; font-weight: 600; color: #8b949e; }}
  .badge {{
    font-size: 12px; font-weight: 600; padding: 2px 8px;
    border-radius: 12px; background: #30363d;
  }}
  .badge.up {{ background: #1a3a2a; color: #3fb950; }}
  .badge.down {{ background: #3d1a1a; color: #f85149; }}
  .price {{ font-size: 26px; font-weight: 700; margin: 6px 0; }}
  .distances {{ margin: 8px 0 6px; }}
  .dist-item {{ font-size: 12px; color: #8b949e; margin: 2px 0; }}
  .dist-item b {{ color: #e6edf3; }}
  .bar-wrap {{
    height: 6px; background: #f8514933; border-radius: 3px; overflow: hidden;
  }}
  .bar {{ height: 100%; border-radius: 3px; transition: width 0.3s; }}
  .bar-safe {{ background: linear-gradient(90deg, #f85149, #d29922, #3fb950); }}
  .section-title {{
    font-size: 13px; font-weight: 600; color: #8b949e;
    margin: 16px 0 8px; text-transform: uppercase; letter-spacing: 1px;
  }}
  .alert {{
    padding: 10px 14px; border-radius: 8px; font-size: 13px;
    margin-bottom: 8px; line-height: 1.5;
  }}
  .alert-critical {{ background: #3d1a1a; border-left: 4px solid #f85149; }}
  .alert-warning {{ background: #3d2e00; border-left: 4px solid #d29922; }}
  .alert-ok {{ background: #1a3a2a; border-left: 4px solid #3fb950; color: #3fb950; text-align:center; }}
  footer {{
    text-align: center; font-size: 11px; color: #484f58;
    margin-top: 20px; padding: 12px 0;
    border-top: 1px solid #30363d;
  }}
</style>
</head>
<body>
  <header>
    <h1>Portfolio Monitor</h1>
    <div class="updated">{status.get('updated_kst', 'N/A')}</div>
  </header>

  <div class="section-title">Market</div>
  {cards_html}

  <div class="section-title">Alerts</div>
  {alerts_html}

  <footer>
    GitHub Actions auto-refresh every 5 min<br>
    Pull down to reload in mobile browser
  </footer>
</body>
</html>"""


# ============================================================
# 메인
# ============================================================

def main():
    print("=" * 50)
    print("  Portfolio Monitor (GitHub Actions)")
    print("=" * 50)

    data = {}

    # 국내 종목 (네이버)
    data["samsung"] = get_naver_price(STOCKS["samsung"]["code"])
    data["hana"] = get_naver_price(STOCKS["hana"]["code"])
    data["kospi"] = get_kospi_index()

    # WTI: 네이버 먼저, 실패 시 Yahoo
    data["wti"] = get_wti_naver()
    if not data["wti"]:
        print("[INFO] 네이버 WTI 실패, Yahoo 시도...")
        data["wti"] = get_wti_yahoo()

    # 환율: 네이버 먼저, 실패 시 Yahoo
    data["usdkrw"] = get_usdkrw_naver()
    if not data["usdkrw"]:
        print("[INFO] 네이버 환율 실패, Yahoo 시도...")
        data["usdkrw"] = get_usdkrw_yahoo()

    # 상태 출력
    for key, val in data.items():
        print(f"  {key}: {val}")

    # 규칙 체크
    alerts = check_rules(data)

    # 텔레그램 알림
    if alerts:
        for a in alerts:
            icon = "🔴" if a["level"] == "critical" else "⚠️"
            send_telegram(f"{icon} {a['msg']}")
        print(f"\n  >> {len(alerts)}건 알림 발송")
    else:
        print("\n  >> 알림 없음 (정상)")

    # status.json 저장
    status = build_status(data, alerts)
    out_dir = Path(__file__).parent / "docs"
    out_dir.mkdir(exist_ok=True)

    with open(out_dir / "status.json", "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)
    print(f"  >> status.json 저장 완료")

    # 대시보드 HTML 생성
    html = generate_dashboard(status)
    with open(out_dir / "index.html", "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  >> index.html 생성 완료")

    # 알림이 있으면 exit code 1 (워크플로우에서 활용 가능)
    # 단, 정상 종료로 처리
    print("\n  완료!")


if __name__ == "__main__":
    main()
