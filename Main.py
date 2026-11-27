#!/usr/bin/env python3
"""
차트 0% — 오로지 실시간 기사·증권사 리포트·수주공시·선물·환율·옵션만기일 기반 추천 엔진
"""
import datetime
import json
import os
import re
import urllib.parse
import xml.etree.ElementTree as ET
import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
}
KST = datetime.timezone(datetime.timedelta(hours=9))

POS_RULES = [
    (r"목표가\s*상향|목표주가\s*상향|목표가\s*2배", 35, "목표가 상향"),
    (r"공급계약|대규모\s*수주|수주\s*호조|수주잔고|수주\s*확대", 28, "수주·계약"),
    (r"최대\s*실적|어닝\s*서프라이즈|영업익.*증가|흑자\s*전환|실적\s*개선|성장\s*견인", 25, "실적 성장"),
    (r"최선호주|탑픽|매수\s*유지|비중\s*확대|강력\s*매수", 22, "증권사 매수"),
    (r"외국인.*순매수|기관.*순매수|쌍끌이|외인.*사자", 20, "외인·기관 수급"),
    (r"자사주|주주환원|밸류업|배당\s*확대|책임경영", 20, "주주환원"),
    (r"급등|상한가|신고가|강세|수혜|기대감|돌파", 12, "강세·모멘텀"),
]
NEG_RULES = [
    (r"유상증자|적자\s*전환|어닝\s*쇼크|거래정지|감사의견|횡령", -40, "중대 악재"),
    (r"목표가\s*하향|목표주가\s*하향|목표가\s*줄하향|매도\s*의견", -22, "목표가 하향"),
    (r"급락|폭락|사이드카|약세|우려|부진", -12, "변동성 주의"),
]


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
    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}?range=5d&interval=1d"
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


def nth_weekday(y, m, wd, n):
    first = datetime.date(y, m, 1)
    offset = (wd - first.weekday() + 7) % 7
    return first + datetime.timedelta(days=offset + (n - 1) * 7)


def check_market_events(today):
    # 한국 옵션만기일: 매월 둘째 주 목요일(weekday=3), 3·6·9·12월은 네 마녀의 날
    y, m = today.year, today.month
    kr_exp = nth_weekday(y, m, 3, 2)
    if kr_exp < today:
        m += 1
        if m > 12:
            m, y = 1, y + 1
        kr_exp = nth_weekday(y, m, 3, 2)
    kr_d = (kr_exp - today).days
    is_quad = kr_exp.month in (3, 6, 9, 12)

    # MSCI 리밸런싱: 2·5·8·11월 마지막 평일
    msci_dt = None
    for add_y in (0, 1):
        for mm in (2, 5, 8, 11):
            nxt_m = datetime.date(today.year + add_y + (1 if mm == 12 else 0), (mm % 12) + 1, 1)
            dt = nxt_m - datetime.timedelta(days=1)
            while dt.weekday() >= 5:
                dt -= datetime.timedelta(days=1)
            if dt >= today:
                msci_dt = dt
                break
        if msci_dt:
            break
    msci_d = (msci_dt - today).days if msci_dt else 99

    warns = []
    scale = 1.0
    if kr_d == 0:
        scale = 0.7
        warns.append("오늘 한국 네 마녀의 날(동시만기) — 동시호가 급변동 주의" if is_quad else "오늘 한국 옵션만기일 — 종가 동시호가 주의")
    elif kr_d == 1:
        warns.append("내일 한국 옵션만기일 — 변동성 대비")
    if msci_d == 0:
        scale = min(scale, 0.75)
        warns.append("오늘 MSCI 리밸런싱일 — 종가 수급 왜곡 주의")

    return {
        "krExpiryLabel": f"{'네 마녀의 날' if is_quad else '옵션만기일'} D-{kr_d} ({kr_exp.strftime('%m/%d')})",
        "msciLabel": f"MSCI 리밸런싱 D-{msci_d} ({msci_dt.strftime('%m/%d')})" if msci_dt else "",
        "eventScale": scale,
        "warns": warns,
    }


def fetch_all_news():
    feeds = [
        ("한국경제", "https://www.hankyung.com/feed/finance"),
        ("매일경제", "https://www.mk.co.kr/rss/50200011/"),
        ("이데일리", "https://www.edaily.co.kr/rss/rss_stock.xml"),
        ("구글·리포트", "https://news.google.com/rss/search?q=" + urllib.parse.quote("목표주가 상향 OR 모닝 리포트 증권 when:2d") + "&hl=ko&gl=KR&ceid=KR:ko"),
        ("구글·수급", "https://news.google.com/rss/search?q=" + urllib.parse.quote("코스피 특징주 OR 외국인 순매수 OR 공급계약 when:2d") + "&hl=ko&gl=KR&ceid=KR:ko"),
    ]
    items, seen = [], set()
    for src, url in feeds:
        try:
            r = requests.get(url, headers=HEADERS, timeout=8)
            if not r.ok:
                continue
            root = ET.fromstring(r.content)
            for it in root.findall(".//item")[:30]:
                raw_t = (it.findtext("title") or "").strip()
                title = re.sub(r"\s+-\s+[^-]+$", "", raw_t).strip()
                link = (it.findtext("link") or "#").strip()
                pub = (it.findtext("pubDate") or "")[:16]
                if not title or title in seen:
                    continue
                seen.add(title)
                pos_s = sum(pts for pat, pts, _ in POS_RULES if re.search(pat, title))
                neg_s = sum(pts for pat, pts, _ in NEG_RULES if re.search(pat, title))
                items.append({
                    "src": src, "title": title, "url": link, "date": pub,
                    "pos": pos_s, "neg": neg_s, "net": pos_s + neg_s
                })
        except Exception:
            continue
    return items


def collect_universe_300():
    urls = [
        ("코스피", "https://m.stock.naver.com/api/stocks/marketValue/KOSPI?page=1&pageSize=100"),
        ("코스피", "https://m.stock.naver.com/api/stocks/marketValue/KOSPI?page=2&pageSize=100"),
        ("코스닥", "https://m.stock.naver.com/api/stocks/marketValue/KOSDAQ?page=1&pageSize=100"),
    ]
    out, seen = [], set()
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
            out.append({
                "code": code, "name": name, "market": market, "px": px,
                "dayChg": round(to_num(st.get("fluctuationsRatio")), 2),
                "tradingVal": to_num(st.get("accumulatedTradingValue")) * 1_000_000,
            })
    return out


def main():
    now_kst = datetime.datetime.now(KST)
    ev_cal = check_market_events(now_kst.date())

    ks_basic = fetch_json("https://m.stock.naver.com/api/index/KOSPI/basic") or {}
    kq_basic = fetch_json("https://m.stock.naver.com/api/index/KOSDAQ/basic") or {}
    fut_ks = fetch_yahoo_change("^KS200")
    fut_nq = fetch_yahoo_change("NQ=F")
    fx_krw = fetch_yahoo_change("KRW=X")

    news_list = fetch_all_news()
    universe = collect_universe_300()

    # 1) 오로지 선물 + 환율 + 뉴스 심리 + 옵션만기일로만 시장 점수(0~100) 산출 (차트 0%)
    pos_news = sum(1 for n in news_list if n["net"] > 0)
    neg_news = sum(1 for n in news_list if n["net"] < 0)
    news_pts = max(10, min(90, 50 + (pos_news - neg_news) * 2))
    fut_avg = fut_ks["pct"] * 0.6 + fut_nq["pct"] * 0.4
    fut_pts = 85 if (fut_avg >= 0.3 and fx_krw["pct"] <= 0.4) else 20 if (fut_avg <= -0.9 or fx_krw["pct"] >= 1.2) else 50
    score = round(fut_pts * 0.6 + news_pts * 0.4)

    if score >= 68:
        r_type, badge = "bull", f"🟢 선물·뉴스 강세 ({ev_cal['krExpiryLabel']})"
        title = "호재·리포트 종목 적극 분할 매수"
    elif score >= 42:
        r_type, badge = "neutral", f"🟡 선물·뉴스 혼조 ({ev_cal['krExpiryLabel']})"
        title = "목표가 상향·수주 확실 종목만 50% 비중"
    else:
        r_type, badge = "bear", f"🔴 선물급락/환율주의 ({ev_cal['krExpiryLabel']})"
        title = "신규 매수 최소화 · 현금 방어 우선"
    if ev_cal["warns"]:
        title = "⚠️ " + ev_cal["warns"][0]

    ks_px, ks_chg = round(to_num(ks_basic.get("closePrice")), 2), round(to_num(ks_basic.get("fluctuationsRatio")), 2)
    kq_px, kq_chg = round(to_num(kq_basic.get("closePrice")), 2), round(to_num(kq_basic.get("fluctuationsRatio")), 2)

    regime = {
        "score": score, "type": r_type, "badge": badge, "title": title,
        "maxSlots": 4 if r_type == "bull" else 2 if r_type == "neutral" else 1,
        "riskScale": (1.0 if r_type == "bull" else 0.5 if r_type == "neutral" else 0.25) * ev_cal["eventScale"],
        "items": [
            {"name": "코스피", "close": ks_px, "chg": ks_chg, "state": ev_cal["krExpiryLabel"]},
            {"name": "코스닥", "close": kq_px, "chg": kq_chg, "state": ev_cal["msciLabel"]},
        ],
    }

    # 2) 오로지 실시간 기사·리포트·수주·선물·만기 기반으로만 300종목 점수화!
    scored = []
    for st in universe:
        nm = st["name"]
        if len(nm) < 2:
            continue
        matched = [n for n in news_list if nm in n["title"]]
        if not matched:
            continue

        pts = 40
        pos_tags, neg_tags = [], []
        for n in matched:
            for pat, p, tg in POS_RULES:
                if re.search(pat, n["title"]):
                    pts += p
                    if tg not in pos_tags:
                        pos_tags.append(tg)
            for pat, p, tg in NEG_RULES:
                if re.search(pat, n["title"]):
                    pts += p
                    if tg not in neg_tags:
                        neg_tags.append(tg)
            pts += 5

        if pts < 55:
            continue

        px = st["px"]
        sl_px = round(px * 0.95)
        tp_px = round(px * 1.10)
        why = [
            f"실시간 뉴스 {len(matched)}건 포착: {', '.join(pos_tags[:3]) if pos_tags else '시장 주목'}",
            f"핵심 기사: {matched[0]['title']}",
            f"선물·만기 반영: 코스피선물 {fut_ks['pct']:+.2f}% · 나스닥선물 {fut_nq['pct']:+.2f}% · {ev_cal['krExpiryLabel']}",
        ]
        if neg_tags:
            why.append(f"⚠️ 주의 키워드 동반({', '.join(neg_tags[:2])}) — 눌림 분할 필수")

        scored.append({
            "code": st["code"], "name": nm, "market": st["market"],
            "px": px, "dayChg": st["dayChg"], "slPx": sl_px, "tpPx": tp_px,
            "ev": round((pts - 50) / 10.0, 1), "winRate": min(95, pts),
            "rs60": round(st["dayChg"], 1), "tradingVal": round(st["tradingVal"] / 1e8),
            "posTags": pos_tags, "why": why, "score": pts,
            "news": [{"src": n["src"], "title": n["title"], "url": n["url"], "date": n["date"]} for n in matched[:3]],
        })

    scored.sort(key=lambda x: x["score"], reverse=True)

    # 3) 전략별 자동 배치 (기사·선물·만기 100% + 오후 눌림목·덜 오른 호재 필터 포함)
    swing = [x for x in scored if any(t in x["posTags"] for t in ("목표가 상향", "실적 성장", "수주·계약", "증권사 매수", "주주환원"))][:8] or scored[:6]
    pullback = [x for x in scored if -2.0 <= x["dayChg"] <= 3.0 and x["score"] >= 60][:10]
    close_bet = [x for x in scored if -1.5 <= x["dayChg"] <= 4.5 and x["score"] >= 60 and ev_cal["eventScale"] > 0.75][:8]
    open_bet = [x for x in scored if -1.0 <= x["dayChg"] <= 5.0][:6]
    nxt = [x for x in scored if x["tradingVal"] >= 300][:6]

    payload = {
        "updatedAt": now_kst.strftime("%Y-%m-%d %H:%M KST (기사·선물·만기 100%)"),
        "scannedTotal": len(universe),
        "analyzedDetail": len(news_list),
        "regime": regime,
        "macro": {"futKospi": fut_ks, "futNasdaq": fut_nq, "fxKrw": fx_krw},
        "swing": swing,
        "pullback": pullback,
        "closeBet": close_bet,
        "openBet": open_bet,
        "nxt": nxt,
        "rankedTable": [{"code": x["code"], "name": x["name"], "market": x["market"], "px": x["px"], "dayChg": x["dayChg"], "rs60": x["score"], "ev": x["ev"]} for x in scored[:40]],
        "news": [{"src": n["src"], "title": n["title"], "url": n["url"], "date": n["date"]} for n in news_list[:40]],
    }

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"Saved news/futures/expiry-only data.json: {len(news_list)} news, {len(scored)} matched stocks.")


if __name__ == "__main__":
    main()
