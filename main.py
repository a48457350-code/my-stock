#!/usr/bin/env python3
"""
국장 300종목 자동 수집·분석·카카오톡 전송 엔진 (GitHub Actions 무료 자동화용)
- 코스피 200 + 코스닥 100 = 300종목 실시간 시세/거래대금 수집 (네이버 금융)
- 수급 상위 45종목 최근 1년 일봉 정밀 분석 (정배열·RS·ATR·기댓값 EV·승률)
- 코스피200·나스닥100 선물(NQ=F)·원/달러 환율(KRW=X) 자동 수집
- 한국경제·매일경제 증권 RSS 실시간 뉴스 수집 및 종목 매칭
- data.json 대용량 저장 → index.html(GitHub Pages) 즉시 반영
- 카카오톡 '나에게 보내기' REST API 자동 전송 (토큰 자동 갱신 포함)
"""
import datetime
import json
import math
import os
import re
import xml.etree.ElementTree as ET
import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
}
KST = datetime.timezone(datetime.timedelta(hours=9))


def to_num(v):
    try:
        return float(str(v or "0").replace(",", "").strip())
    except Exception:
        return 0.0


def fetch_json(url, timeout=10):
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        if r.ok:
            return r.json()
    except Exception:
        pass
    return None


def fetch_yahoo_change(sym):
    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{sym}?range=5d&interval=1d"
    j = fetch_json(url)
    try:
        m = j["chart"]["result"][0]["meta"]
        px = float(m.get("regularMarketPrice") or 0)
        pct = m.get("regularMarketChangePercent")
        if pct is None and px > 0 and m.get("chartPreviousClose"):
            pct = (px / float(m["chartPreviousClose"]) - 1.0) * 100.0
        return {"px": round(px, 2), "pct": round(float(pct or 0), 2)}
    except Exception:
        return {"px": 0.0, "pct": 0.0}


def fetch_naver_sise(symbol, days=370):
    now = datetime.datetime.now(KST)
    start = (now - datetime.timedelta(days=days)).strftime("%Y%m%d")
    end = now.strftime("%Y%m%d")
    url = f"https://api.finance.naver.com/siseJson.naver?symbol={symbol}&requestType=1&startTime={start}&endTime={end}&timeframe=day"
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        if not r.ok:
            return None
        rows = json.loads(r.text.replace("'", '"').strip())
        if not isinstance(rows, list) or len(rows) < 70:
            return None
        o, h, l, c, v = [], [], [], [], []
        for row in rows[1:]:
            if len(row) < 6:
                continue
            cc = float(row[4])
            if cc <= 0:
                continue
            o.append(float(row[1]) if float(row[1]) > 0 else cc)
            h.append(float(row[2]) if float(row[2]) > 0 else cc)
            l.append(float(row[3]) if float(row[3]) > 0 else cc)
            c.append(cc)
            v.append(float(row[5]) or 0.0)
        return {"o": o, "h": h, "l": l, "c": c, "v": v} if len(c) >= 65 else None
    except Exception:
        return None


def sma(arr, p):
    out = [None] * len(arr)
    s = 0.0
    for i, val in enumerate(arr):
        s += val
        if i >= p:
            s -= arr[i - p]
        if i >= p - 1:
            out[i] = s / p
    return out


def atr(h, l, c, p=14):
    n = len(c)
    tr = [0.0] * n
    for i in range(n):
        tr[i] = h[i] - l[i] if i == 0 else max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
    out = [None] * n
    if n < p:
        return out
    prev = sum(tr[:p]) / p
    out[p - 1] = prev
    for i in range(p, n):
        prev = (prev * (p - 1) + tr[i]) / p
        out[i] = prev
    return out


def fetch_rss_news():
    feeds = [
        ("한국경제", "https://www.hankyung.com/feed/finance"),
        ("매일경제", "https://www.mk.co.kr/rss/50200011/"),
    ]
    news = []
    seen = set()
    for src, url in feeds:
        try:
            r = requests.get(url, headers=HEADERS, timeout=8)
            if not r.ok:
                continue
            root = ET.fromstring(r.content)
            for item in root.findall(".//item")[:25]:
                title = (item.findtext("title") or "").strip()
                link = (item.findtext("link") or "#").strip()
                pub = (item.findtext("pubDate") or "")[:16]
                if not title or title in seen:
                    continue
                seen.add(title)
                news.append({"src": src, "title": title, "url": link, "date": pub})
        except Exception:
            continue
    return news


def collect_universe():
    urls = [
        ("코스피", "https://m.stock.naver.com/api/stocks/marketValue/KOSPI?page=1&pageSize=100"),
        ("코스피", "https://m.stock.naver.com/api/stocks/marketValue/KOSPI?page=2&pageSize=100"),
        ("코스닥", "https://m.stock.naver.com/api/stocks/marketValue/KOSDAQ?page=1&pageSize=100"),
    ]
    universe = []
    seen = set()
    for market, url in urls:
        j = fetch_json(url)
        for st in (j or {}).get("stocks", []):
            code = str(st.get("itemCode") or "").strip()
            name = str(st.get("stockName") or "").strip()
            if not re.fullmatch(r"\d{6}", code) or code in seen:
                continue
            if re.search(r"(우$|우B$|스팩|SPAC|ETF|ETN)", name):
                continue
            px = to_num(st.get("closePrice"))
            if px <= 0:
                continue
            seen.add(code)
            universe.append({
                "code": code,
                "name": name,
                "market": market,
                "px": px,
                "chgPct": to_num(st.get("fluctuationsRatio")),
                "vol": to_num(st.get("accumulatedTradingVolume")),
                "tradingVal": to_num(st.get("accumulatedTradingValue")) * 1_000_000,
            })
    return universe


def send_kakao_memo(summary_text, web_url):
    """카카오톡 나에게 보내기 REST API (Refresh Token으로 Access Token 자동 갱신 후 발송)"""
    rest_key = os.environ.get("KAKAO_REST_API_KEY", "").strip()
    refresh_token = os.environ.get("KAKAO_REFRESH_TOKEN", "").strip()
    if not rest_key or not refresh_token:
        print("Kakao secrets not set; skipping KakaoTalk push.")
        return False
    try:
        tok = requests.post(
            "https://kauth.kakao.com/oauth/token",
            data={
                "grant_type": "refresh_token",
                "client_id": rest_key,
                "refresh_token": refresh_token,
            },
            timeout=10,
        ).json()
        access_token = tok.get("access_token")
        if not access_token:
            print("Kakao token refresh failed:", tok)
            return False

        template = {
            "object_type": "text",
            "text": summary_text[:950],
            "link": {"web_url": web_url, "mobile_web_url": web_url},
            "button_title": "대시보드 전체 보기",
        }
        res = requests.post(
            "https://kapi.kakao.com/v2/api/talk/memo/default/send",
            headers={"Authorization": f"Bearer {access_token}"},
            data={"template_object": json.dumps(template, ensure_ascii=False)},
            timeout=10,
        )
        print("KakaoTalk send status:", res.status_code, res.text[:120])
        return res.ok
    except Exception as e:
        print("KakaoTalk error:", e)
        return False


def main():
    now_kst = datetime.datetime.now(KST)
    ks_basic = fetch_json("https://m.stock.naver.com/api/index/KOSPI/basic") or {}
    kq_basic = fetch_json("https://m.stock.naver.com/api/index/KOSDAQ/basic") or {}
    ks_ser = fetch_naver_sise("KOSPI")
    kq_ser = fetch_naver_sise("KOSDAQ")
    fut_ks = fetch_yahoo_change("^KS200")
    fut_nq = fetch_yahoo_change("NQ=F")
    fx_krw = fetch_yahoo_change("KRW=X")
    news_list = fetch_rss_news()

    def idx_info(name, basic, ser):
        px = to_num(basic.get("closePrice")) or (ser["c"][-1] if ser else 0)
        chg = to_num(basic.get("fluctuationsRatio"))
        pts = 0
        if ser and len(ser["c"]) >= 65:
            c = ser["c"][:]
            if px > 0:
                c[-1] = px
            s20, s60 = sma(c, 20), sma(c, 60)
            if c[-1] > s60[-1]:
                pts += 50
            if c[-1] > s20[-1]:
                pts += 25
            if s20[-1] > s20[-6]:
                pts += 25
        state = "상승추세" if pts >= 75 else "혼조/눌림" if pts >= 50 else "하락주의"
        return {"name": name, "close": round(px, 2), "chg": round(chg, 2), "pts": pts, "state": state}

    ks_i = idx_info("코스피", ks_basic, ks_ser)
    kq_i = idx_info("코스닥", kq_basic, kq_ser)
    score = round((ks_i["pts"] + kq_i["pts"]) / 2)
    if score >= 70:
        regime = {"score": score, "type": "bull", "badge": "🟢 상승장 · 적극 운용", "title": "주도주 분할 매수 가능 (주식 80%)", "maxSlots": 4, "riskScale": 1.0, "items": [ks_i, kq_i]}
    elif score >= 40:
        regime = {"score": score, "type": "neutral", "badge": "🟡 혼조장 · 선별 방어", "title": "실적·수급 대장주 1~2개 선별 (주식 40%)", "maxSlots": 2, "riskScale": 0.5, "items": [ks_i, kq_i]}
    else:
        regime = {"score": score, "type": "bear", "badge": "🔴 하락장 · 현금 방어", "title": "스윙 신규 매수 중단 · 현금 확보", "maxSlots": 0, "riskScale": 0.0, "items": [ks_i, kq_i]}

    universe = collect_universe()
    by_val = sorted(universe, key=lambda x: x["tradingVal"], reverse=True)
    shortlist, picked_codes = [], set()
    for s in by_val[:30]:
        picked_codes.add(s["code"])
        shortlist.append(s)
    for s in sorted([u for u in universe if u["tradingVal"] >= 15e9 and u["chgPct"] > 0.5], key=lambda x: x["chgPct"] * math.log10(x["tradingVal"] + 1), reverse=True):
        if len(shortlist) < 45 and s["code"] not in picked_codes:
            picked_codes.add(s["code"])
            shortlist.append(s)

    ks_ret60 = (ks_ser["c"][-1] / ks_ser["c"][-61] - 1) * 100 if ks_ser and len(ks_ser["c"]) > 61 else 0.0
    swing, close_bet, open_bet, nxt, table = [], [], [], [], []

    for st in shortlist:
        d = fetch_naver_sise(st["code"])
        if not d:
            continue
        c, h, l, o, v = d["c"], d["h"], d["l"], d["o"], d["v"]
        last = len(c) - 1
        c[last] = st["px"]
        s20, s60, a14 = sma(c, 20), sma(c, 60), atr(h, l, c, 14)
        px, prev, op = c[last], c[last - 1], o[last]
        day_chg = st["chgPct"] or ((px / prev - 1) * 100)
        gap_now = (op / prev - 1) * 100
        ret60 = (px / c[last - 60] - 1) * 100
        rs60 = ret60 - ks_ret60
        run5 = (px / c[last - 5] - 1) * 100
        above20 = s20[last] is not None and px > s20[last]
        above60 = s60[last] is not None and px > s60[last]
        align = above60 and s20[last] is not None and s20[last] > s60[last]
        atr_pct = ((a14[last] or px * 0.025) / px) * 100
        vs20 = sum(v[last - 19 : last + 1]) / 20
        vol_mult = v[last] / vs20 if vs20 > 0 else 1.0
        hi20 = max(h[last - 20 : last])
        near_hi20 = hi20 > 0 and px >= hi20 * 0.975

        sl_pct = max(3.0, min(8.0, atr_pct * 2.0))
        tp_pct = sl_pct * 2.0
        sl_px = round(px * (1 - sl_pct / 100))
        tp_px = round(px * (1 + tp_pct / 100))

        # 1년 동일 자리 기댓값(EV) 검증
        cnt = wins = 0
        tot_r = 0.0
        i = 61
        while i < last - 12:
            if s60[i] and s20[i] and c[i] > s60[i] and s20[i] > s60[i]:
                d20 = (c[i] / s20[i] - 1) * 100
                if -3 <= d20 <= 7:
                    en = o[i + 1] if o[i + 1] > 0 else c[i + 1]
                    sl_b, tp_b = en * (1 - sl_pct / 100), en * (1 + tp_pct / 100)
                    ex = c[min(i + 15, last)]
                    for j in range(i + 2, min(i + 16, last + 1)):
                        if l[j] <= sl_b:
                            ex = sl_b; break
                        if h[j] >= tp_b:
                            ex = tp_b; break
                    rr = (ex / en - 1) * 100 - 0.35
                    cnt += 1
                    if rr > 0:
                        wins += 1
                    tot_r += rr
                    i += 5
            i += 1
        ev = round(tot_r / cnt, 2) if cnt >= 2 else 0.0
        wr = round(wins / cnt * 100, 1) if cnt >= 2 else 0.0

        matched_news = [n for n in news_list if st["name"] in n["title"]][:2]
        table.append({
            "code": st["code"], "name": st["name"], "market": st["market"],
            "px": px, "dayChg": round(day_chg, 2), "ret60": round(ret60, 1),
            "rs60": round(rs60, 1), "ev": ev, "tradingVal": round(st["tradingVal"] / 1e8),
        })

        base_obj = {
            "code": st["code"], "name": st["name"], "market": st["market"],
            "px": px, "dayChg": round(day_chg, 2), "slPx": sl_px, "tpPx": tp_px,
            "slPct": round(sl_pct, 1), "tpPct": round(tp_pct, 1),
            "ev": ev, "winRate": wr, "btCnt": cnt, "rs60": round(rs60, 1),
            "volMult": round(vol_mult, 1), "news": matched_news,
        }

        if align and rs60 > 0 and run5 < 11 and (cnt < 2 or ev > 0):
            swing.append({
                **base_obj,
                "why": [
                    f"20>60 정배열 + 최근 60일 코스피 대비 RS {rs60:+.1f}% 주도주",
                    f"과거 1년 유사 자리 {cnt}회 검증 승률 {wr:.0f}% · 비용 후 기댓값 {ev:+.2f}%",
                    f"손절 -{sl_pct:.1f}%({sl_px:,}원) · 1차 반절익절 +{tp_pct:.1f}%({tp_px:,}원)",
                ],
                "score": 30 + min(30, rs60 * 1.3) + (15 if near_hi20 else 0) + min(25, ev * 5),
            })

        if day_chg >= 1.5 and vol_mult >= 1.3 and above20:
            close_bet.append({
                **base_obj,
                "slPx": round(px * 0.95), "tpPx": round(px * 1.03),
                "why": [
                    f"거래량 20일 평균의 {vol_mult:.1f}배 + 당일 {day_chg:+.1f}% 강세 마감",
                    "15:20~15:30 동시호가 소액 분산 매수 → 익일 09:00 시초가 전량 매도",
                ],
                "score": (35 if near_hi20 else 10) + vol_mult * 12 + day_chg * 3,
            })

        if above60 and day_chg > 0 and -0.5 <= gap_now <= 5.0:
            open_bet.append({
                **base_obj,
                "slPx": round(px * 0.975), "tpPx": round(px * 1.035),
                "why": [
                    f"60일선 상승 추세 + 건강한 시초 갭 {gap_now:+.1f}% (+6%↑ 과열 아님)",
                    "09:00 진입 → 당일 +3.5% 익절 / -2.5% 손절 / 15:20 전 청산",
                ],
                "score": 30 + vol_mult * 10 + day_chg * 2 + ev * 5,
            })

        if above20 and st["tradingVal"] >= 50e9 and day_chg > -1.0:
            nxt.append({
                **base_obj,
                "slPx": round(px * 0.965), "tpPx": round(px * 1.045),
                "why": [
                    f"일 거래대금 {round(st['tradingVal']/1e8):,}억 대형주 (NXT 호가 안정)",
                    "08:00~08:50 프리 / 15:40~20:00 애프터 지정가 50% 비중 진입",
                ],
                "score": (30 if align else 10) + min(30, st["tradingVal"] / 50e9) + day_chg * 2,
            })

    swing.sort(key=lambda x: x["score"], reverse=True)
    close_bet.sort(key=lambda x: x["score"], reverse=True)
    open_bet.sort(key=lambda x: x["score"], reverse=True)
    nxt.sort(key=lambda x: x["score"], reverse=True)
    table.sort(key=lambda x: x["rs60"], reverse=True)

    payload = {
        "updatedAt": now_kst.strftime("%Y-%m-%d %H:%M KST"),
        "scannedTotal": len(universe),
        "analyzedDetail": len(shortlist),
        "regime": regime,
        "macro": {"futKospi": fut_ks, "futNasdaq": fut_nq, "fxKrw": fx_krw},
        "swing": swing[:6],
        "closeBet": close_bet[:6],
        "openBet": open_bet[:6],
        "nxt": nxt[:6],
        "rankedTable": table[:50],
        "allUniverseCount": len(universe),
        "news": news_list[:30],
    }

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"Saved data.json: {len(universe)} stocks scanned, {len(swing)} swing picks.")

    # 카카오톡 자동 알림 메시지 구성
    top_lines = [f"• {p['name']} ({p['px']:,}원, EV {p['ev']:+.1f}%)" for p in swing[:3]]
    msg = (
        f"📈 [시드빌더 {now_kst.strftime('%m/%d %H:%M')} 자동 브리핑]\n"
        f"장세: {regime['badge']} ({regime['score']}점)\n"
        f"코스피 {ks_i['close']:,.1f}({ks_i['chg']:+.2f}%) · 코스닥 {kq_i['close']:,.1f}({kq_i['chg']:+.2f}%)\n"
        f"나스닥선물 {fut_nq['pct']:+.2f}% · 환율 {fx_krw['pct']:+.2f}%\n\n"
        f"🔥 오늘의 스윙 Top:\n" + ("\n".join(top_lines) if top_lines else "• 조건 통과 종목 없음 (현금 방어)")
    )
    web_url = os.environ.get("DASHBOARD_URL", "https://github.com")
    send_kakao_memo(msg, web_url)


if __name__ == "__main__":
    main()
