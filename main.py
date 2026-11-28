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


US_UNIVERSE = [
    # 빅테크 · AI · 클라우드 (16)
    ("AAPL", "애플", ["AAPL", "Apple", "애플"], "빅테크"),
    ("MSFT", "마이크로소프트", ["MSFT", "Microsoft", "마이크로소프트"], "빅테크·AI"),
    ("AMZN", "아마존", ["AMZN", "Amazon", "아마존"], "클라우드·커머스"),
    ("GOOGL", "알파벳(구글)", ["GOOGL", "GOOG", "Alphabet", "Google", "알파벳", "구글"], "빅테크·AI"),
    ("META", "메타", ["META", "Meta Platforms", "메타"], "빅테크·AI"),
    ("ORCL", "오라클", ["ORCL", "Oracle", "오라클"], "AI·클라우드"),
    ("IBM", "IBM", ["IBM"], "AI·엔터프라이즈"),
    ("CSCO", "시스코", ["CSCO", "Cisco", "시스코"], "네트워크"),
    ("PLTR", "팔란티어", ["PLTR", "Palantir", "팔란티어"], "AI·소프트웨어"),
    ("CRM", "세일즈포스", ["CRM", "Salesforce", "세일즈포스"], "클라우드·SW"),
    ("NOW", "서비스나우", ["ServiceNow", "서비스나우"], "AI·클라우드"),
    ("ADBE", "어도비", ["ADBE", "Adobe", "어도비"], "소프트웨어"),
    ("INTU", "인튜이트", ["INTU", "Intuit", "인튜이트"], "소프트웨어"),
    ("SNOW", "스노우플레이크", ["Snowflake", "스노우플레이크"], "데이터·AI"),
    ("DDOG", "데이터독", ["DDOG", "Datadog", "데이터독"], "클라우드·AI"),
    ("MDB", "몽고DB", ["MDB", "MongoDB", "몽고DB"], "데이터·AI"),
    # 반도체 · AI 인프라 (20)
    ("NVDA", "엔비디아", ["NVDA", "Nvidia", "NVIDIA", "엔비디아"], "AI·반도체"),
    ("AVGO", "브로드컴", ["AVGO", "Broadcom", "브로드컴"], "AI·반도체"),
    ("TSM", "TSMC", ["TSM", "TSMC", "Taiwan Semiconductor"], "파운드리"),
    ("AMD", "AMD", ["AMD", "Advanced Micro Devices"], "AI·반도체"),
    ("MU", "마이크론", ["MU", "Micron", "마이크론"], "메모리·HBM"),
    ("QCOM", "퀄컴", ["QCOM", "Qualcomm", "퀄컴"], "반도체"),
    ("INTC", "인텔", ["INTC", "Intel", "인텔"], "반도체"),
    ("TXN", "텍사스인스트루먼트", ["TXN", "Texas Instruments"], "반도체"),
    ("AMAT", "어플라이드머티리얼즈", ["AMAT", "Applied Materials"], "반도체 장비"),
    ("LRCX", "램리서치", ["LRCX", "Lam Research", "램리서치"], "반도체 장비"),
    ("KLAC", "KLA", ["KLAC", "KLA Corp"], "반도체 장비"),
    ("ASML", "ASML", ["ASML"], "반도체 장비"),
    ("ARM", "ARM", ["Arm Holdings", "ARM"], "반도체 설계"),
    ("MRVL", "마벨테크놀로지", ["MRVL", "Marvell", "마벨"], "AI·반도체"),
    ("ADI", "아날로그디바이스", ["ADI", "Analog Devices"], "반도체"),
    ("NXPI", "NXP반도체", ["NXPI", "NXP"], "차량용 반도체"),
    ("ON", "온세미컨덕터", ["ON Semiconductor", "onsemi"], "전력 반도체"),
    ("SMCI", "슈퍼마이크로", ["SMCI", "Super Micro", "슈퍼마이크로"], "AI 서버"),
    ("DELL", "델테크놀로지", ["DELL", "Dell Technologies", "델테크놀로지"], "AI 서버"),
    ("VRT", "버티브홀딩스", ["VRT", "Vertiv", "버티브"], "AI 데이터센터"),
    # 사이버보안 · 광고 · 플랫폼 (12)
    ("PANW", "팔로알토", ["PANW", "Palo Alto", "팔로알토"], "사이버보안"),
    ("CRWD", "크라우드스트라이크", ["CRWD", "CrowdStrike", "크라우드스트라이크"], "사이버보안"),
    ("FTNT", "포티넷", ["FTNT", "Fortinet", "포티넷"], "사이버보안"),
    ("ZS", "지스케일러", ["Zscaler", "지스케일러"], "사이버보안"),
    ("NET", "클라우드플레어", ["Cloudflare", "클라우드플레어"], "클라우드·보안"),
    ("APP", "앱러빈", ["AppLovin", "앱러빈"], "AI·광고"),
    ("TTD", "트레이드데스크", ["TTD", "Trade Desk", "트레이드데스크"], "애드테크"),
    ("NFLX", "넷플릭스", ["NFLX", "Netflix", "넷플릭스"], "미디어"),
    ("SPOT", "스포티파이", ["SPOT", "Spotify", "스포티파이"], "미디어"),
    ("DIS", "디즈니", ["Disney", "Walt Disney", "디즈니"], "미디어·엔터"),
    ("UBER", "우버", ["UBER", "Uber", "우버"], "플랫폼·자율주행"),
    ("ABNB", "에어비앤비", ["ABNB", "Airbnb", "에어비앤비"], "플랫폼·여행"),
    # 전기차 · 로봇 · 양자 · 우주 · 방산 (14)
    ("TSLA", "테슬라", ["TSLA", "Tesla", "테슬라"], "전기차·로봇"),
    ("RIVN", "리비안", ["RIVN", "Rivian", "리비안"], "전기차"),
    ("GM", "제너럴모터스", ["General Motors", "제너럴모터스"], "자동차"),
    ("F", "포드", ["Ford Motor", "포드"], "자동차"),
    ("IONQ", "아이온큐", ["IONQ", "IonQ", "아이온큐"], "양자컴퓨터"),
    ("RGTI", "리게티컴퓨팅", ["RGTI", "Rigetti", "리게티"], "양자컴퓨터"),
    ("RKLB", "로켓랩", ["RKLB", "Rocket Lab", "로켓랩"], "우주·방산"),
    ("ASTS", "AST스페이스모바일", ["ASTS", "AST SpaceMobile"], "우주통신"),
    ("LMT", "록히드마틴", ["LMT", "Lockheed Martin", "록히드마틴"], "방산"),
    ("RTX", "RTX(레이시온)", ["RTX", "Raytheon", "레이시온"], "방산·항공"),
    ("NOC", "노스롭그루먼", ["NOC", "Northrop Grumman"], "방산"),
    ("GD", "제너럴다이내믹스", ["General Dynamics"], "방산"),
    ("BA", "보잉", ["Boeing", "보잉"], "항공·방산"),
    ("GE", "GE에어로스페이스", ["GE Aerospace"], "항공엔진"),
    # 원전 · 전력 · 에너지 · 산업재 (12)
    ("CEG", "컨스텔레이션에너지", ["CEG", "Constellation Energy", "컨스텔레이션"], "원전·전력"),
    ("VST", "비스트라", ["VST", "Vistra", "비스트라"], "전력·에너지"),
    ("GEV", "GE버노바", ["GEV", "GE Vernova", "GE버노바"], "전력기기"),
    ("SMR", "뉴스케일파워", ["SMR", "NuScale", "뉴스케일"], "소형원전"),
    ("OKLO", "오클로", ["OKLO", "Oklo", "오클로"], "소형원전"),
    ("NEE", "넥스트에라에너지", ["NEE", "NextEra Energy"], "신재생·전력"),
    ("ETN", "이튼", ["ETN", "Eaton"], "전력기기"),
    ("CAT", "캐터필러", ["Caterpillar", "캐터필러"], "산업재·인프라"),
    ("DE", "디어앤컴퍼니", ["John Deere", "Deere & Co"], "농기계·로봇"),
    ("HON", "허니웰", ["HON", "Honeywell", "허니웰"], "산업재"),
    ("XOM", "엑슨모빌", ["XOM", "Exxon", "ExxonMobil", "엑슨모빌"], "에너지"),
    ("CVX", "셰브론", ["CVX", "Chevron", "셰브론"], "에너지"),
    # 헬스케어 · 바이오 · 비만치료 (12)
    ("LLY", "일라이릴리", ["LLY", "Eli Lilly", "Lilly", "일라이릴리"], "비만치료·바이오"),
    ("NVO", "노보노디스크", ["NVO", "Novo Nordisk", "노보노디스크"], "비만치료·바이오"),
    ("UNH", "유나이티드헬스", ["UNH", "UnitedHealth", "유나이티드헬스"], "헬스케어"),
    ("JNJ", "존슨앤드존슨", ["JNJ", "Johnson & Johnson", "존슨앤드존슨"], "헬스케어"),
    ("ABBV", "애브비", ["ABBV", "AbbVie", "애브비"], "바이오·제약"),
    ("MRK", "머크", ["MRK", "Merck", "머크"], "제약"),
    ("PFE", "화이자", ["PFE", "Pfizer", "화이자"], "제약"),
    ("AMGN", "암젠", ["AMGN", "Amgen", "암젠"], "바이오"),
    ("GILD", "길리어드사이언스", ["GILD", "Gilead", "길리어드"], "바이오"),
    ("ISRG", "인튜이티브서지컬", ["ISRG", "Intuitive Surgical"], "의료로봇"),
    ("TMO", "써모피셔", ["TMO", "Thermo Fisher"], "바이오장비"),
    ("VRTX", "버텍스파마", ["VRTX", "Vertex Pharmaceuticals"], "바이오"),
    # 금융 · 핀테크 · 가상자산 (16)
    ("JPM", "JP모건", ["JPM", "JPMorgan", "JP모건"], "금융"),
    ("BAC", "뱅크오브아메리카", ["Bank of America", "뱅크오브아메리카"], "금융"),
    ("WFC", "웰스파고", ["WFC", "Wells Fargo", "웰스파고"], "금융"),
    ("GS", "골드만삭스", ["Goldman Sachs", "골드만삭스"], "투자은행"),
    ("MS", "모건스탠리", ["Morgan Stanley", "모건스탠리"], "투자은행"),
    ("BLK", "블랙록", ["BLK", "BlackRock", "블랙록"], "자산운용"),
    ("BRK-B", "버크셔해서웨이", ["Berkshire Hathaway", "버크셔해서웨이", "버크셔"], "지주·금융"),
    ("V", "비자", ["Visa", "비자"], "결제"),
    ("MA", "마스터카드", ["Mastercard", "마스터카드"], "결제"),
    ("AXP", "아메리칸익스프레스", ["AXP", "American Express"], "결제"),
    ("PYPL", "페이팔", ["PYPL", "PayPal", "페이팔"], "핀테크"),
    ("SQ", "블록(스퀘어)", ["Block Inc", "Cash App"], "핀테크"),
    ("SOFI", "소파이", ["SOFI", "SoFi", "소파이"], "핀테크"),
    ("HOOD", "로빈후드", ["HOOD", "Robinhood", "로빈후드"], "핀테크"),
    ("COIN", "코인베이스", ["COIN", "Coinbase", "코인베이스"], "가상자산"),
    ("MSTR", "스트래티지", ["MSTR", "MicroStrategy", "마이크로스트래티지", "스트래티지"], "가상자산"),
    # 유통 · 소비재 · 통신 · 커머스 (18)
    ("WMT", "월마트", ["WMT", "Walmart", "월마트"], "유통·소비"),
    ("COST", "코스트코", ["COST", "Costco", "코스트코"], "유통·소비"),
    ("HD", "홈디포", ["Home Depot", "홈디포"], "유통·리테일"),
    ("TGT", "타겟", ["Target Corp", "타겟"], "유통·리테일"),
    ("PG", "프록터앤갬블", ["Procter & Gamble", "P&G"], "필수소비재"),
    ("KO", "코카콜라", ["Coca-Cola", "코카콜라"], "필수소비재"),
    ("PEP", "펩시코", ["PepsiCo", "펩시코"], "필수소비재"),
    ("MCD", "맥도날드", ["MCD", "McDonald", "맥도날드"], "외식·소비"),
    ("SBUX", "스타벅스", ["SBUX", "Starbucks", "스타벅스"], "외식·소비"),
    ("NKE", "나이키", ["NKE", "Nike", "나이키"], "의류·소비"),
    ("CMG", "치폴레", ["CMG", "Chipotle", "치폴레"], "외식·소비"),
    ("SHOP", "쇼피파이", ["SHOP", "Shopify", "쇼피파이"], "이커머스"),
    ("MELI", "메르카도리브레", ["MELI", "MercadoLibre"], "이커머스"),
    ("CPNG", "쿠팡", ["CPNG", "Coupang", "쿠팡"], "이커머스"),
    ("DASH", "도어대시", ["DASH", "DoorDash", "도어대시"], "플랫폼"),
    ("TMUS", "T모바일", ["TMUS", "T-Mobile"], "통신"),
    ("VZ", "버라이즌", ["Verizon", "버라이즌"], "통신"),
    ("T", "AT&T", ["AT&T"], "통신"),
]

US_POS_RULES = [
    (r"price target (?:raised|hiked|lifted|boosted)|raises (?:price )?target|PT raised|목표가\s*상향|목표주가\s*상향", 32, "월가 목표가 상향"),
    (r"upgrade[sd]? to (?:buy|outperform|overweight)|top pick|strong buy|initiates.*buy|투자의견\s*상향|최선호주|매수\s*의견", 24, "월가 매수·상향"),
    (r"earnings beat|beats? (?:estimates|expectations|q[1-4])|raises? guidance|record revenue|어닝\s*서프라이즈|실적\s*호조|최대\s*실적", 25, "실적 서프라이즈"),
    (r"contract|partnership|deal|buyback|ai demand|ai chip|data center|수주|공급계약|자사주", 20, "AI·수주·환원"),
    (r"surge[sd]?|rall(?:y|ies)|soar[sd]?|jump[sd]?|record high|급등|강세|신고가", 12, "강세 모멘텀"),
]
US_NEG_RULES = [
    (r"downgrade[sd]?|cut[s]? price target|underweight|sell rating|목표가\s*하향|투자의견\s*하향", -25, "월가 하향 주의"),
    (r"earnings miss|misses estimates|cuts? guidance|lawsuit|probe|recall|tariff hit|어닝\s*쇼크|실적\s*부진|급락|폭락", -28, "악재·변동성 주의"),
]


def fetch_us_news():
    feeds = [
        ("Yahoo·US", "https://feeds.finance.yahoo.com/rss/2.0/headline?s=NVDA,AAPL,MSFT,AMZN,META,GOOGL,TSLA,AVGO,AMD,PLTR,MU,LLY,ORCL,CEG,COIN,JPM,WMT,NFLX,ARM,CRWD&region=US&lang=en-US"),
        ("WallSt·PT", "https://news.google.com/rss/search?q=" + urllib.parse.quote('("price target" raised OR upgraded to buy OR "earnings beat") stock when:2d') + "&hl=en-US&gl=US&ceid=US:en"),
        ("US·Movers", "https://news.google.com/rss/search?q=" + urllib.parse.quote('(NASDAQ OR NYSE) (surges OR rallies OR "top pick" OR "beats estimates") when:2d') + "&hl=en-US&gl=US&ceid=US:en"),
        ("CNBC·US", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=15839135"),
        ("구글·미장", "https://news.google.com/rss/search?q=" + urllib.parse.quote("미국증시 특징주 OR 월가 목표가 상향 OR 서학개미 실적 when:2d") + "&hl=ko&gl=KR&ceid=KR:ko"),
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
                pos_s = sum(pts for pat, pts, _ in US_POS_RULES if re.search(pat, title, re.IGNORECASE))
                neg_s = sum(pts for pat, pts, _ in US_NEG_RULES if re.search(pat, title, re.IGNORECASE))
                items.append({
                    "src": src, "title": title, "url": link, "date": pub,
                    "pos": pos_s, "neg": neg_s, "net": pos_s + neg_s
                })
        except Exception:
            continue
    return items


def build_us_section(now_kst):
    sp500 = fetch_yahoo_change("^GSPC")
    nasdaq = fetch_yahoo_change("^IXIC")
    sox = fetch_yahoo_change("^SOX")
    vix = fetch_yahoo_change("^VIX")
    us_news = fetch_us_news()

    pos_cnt = sum(1 for n in us_news if n["net"] > 0)
    neg_cnt = sum(1 for n in us_news if n["net"] < 0)
    news_pts = max(15, min(90, 50 + (pos_cnt - neg_cnt) * 3))
    idx_avg = sp500["pct"] * 0.35 + nasdaq["pct"] * 0.40 + sox["pct"] * 0.25
    vix_pen = -15 if vix["px"] >= 25 else (-7 if vix["px"] >= 20 else 5)
    idx_pts = max(10, min(95, round(55 + idx_avg * 18 + vix_pen)))
    us_score = max(10, min(95, round(idx_pts * 0.6 + news_pts * 0.4)))

    if us_score >= 66:
        u_type, u_badge = "bull", "🟢 미장 지수·뉴스 강세"
        u_title = "월가 상향·실적주 적극 분할 매수"
    elif us_score >= 42:
        u_type, u_badge = "neutral", "🟡 미장 혼조 · 선별 대응"
        u_title = "목표가 상향·덜 오른 실적주만 50% 분할"
    else:
        u_type, u_badge = "bear", "🔴 미장 약세/변동성 경계"
        u_title = "추격 금지 · VIX 진정 및 현금 방어 우선"

    scored_us = []
    for ticker, kr_name, aliases, sector in US_UNIVERSE:
        matched = []
        for n in us_news:
            t = n["title"]
            hit = False
            for al in aliases:
                if al.isascii() and len(al) <= 5:
                    if re.search(rf"(?<![A-Za-z]){re.escape(al)}(?![A-Za-z])", t):
                        hit = True
                        break
                else:
                    if al.lower() in t.lower():
                        hit = True
                        break
            if hit:
                matched.append(n)
        if not matched:
            continue

        pts = 44
        pos_tags, neg_tags = [], []
        for n in matched:
            for pat, p, tg in US_POS_RULES:
                if re.search(pat, n["title"], re.IGNORECASE):
                    pts += p
                    if tg not in pos_tags:
                        pos_tags.append(tg)
            for pat, p, tg in US_NEG_RULES:
                if re.search(pat, n["title"], re.IGNORECASE):
                    pts += p
                    if tg not in neg_tags:
                        neg_tags.append(tg)
            pts += 5

        if pts < 56:
            continue

        scored_us.append({
            "code": ticker,
            "name": kr_name,
            "sector": sector,
            "posTags": pos_tags,
            "negTags": neg_tags,
            "matched": matched,
            "score": min(99, pts),
        })

    scored_us.sort(key=lambda x: x["score"], reverse=True)
    final_us = []
    for item in scored_us[:18]:
        q = fetch_yahoo_change(item["code"])
        px = q["px"]
        chg = q["pct"]
        sl_px = round(px * 0.94, 2) if px > 0 else 0
        tp_px = round(px * 1.12, 2) if px > 0 else 0
        matched = item["matched"]
        pos_tags = item["posTags"]
        neg_tags = item["negTags"]
        why = [
            f"미국 실시간 기사 {len(matched)}건 포착: {', '.join(pos_tags[:3]) if pos_tags else '월가 주목'}",
            f"핵심 기사: {matched[0]['title']}",
            f"미국 지수 반영: S&P500 {sp500['pct']:+.2f}% · 나스닥 {nasdaq['pct']:+.2f}% · 필라반도체 {sox['pct']:+.2f}%",
        ]
        if neg_tags:
            why.append(f"⚠️ 주의 키워드 동반({', '.join(neg_tags[:2])}) — 눌림 분할 필수")
        final_us.append({
            "code": item["code"],
            "name": item["name"],
            "sector": item["sector"],
            "px": px,
            "dayChg": chg,
            "slPx": sl_px,
            "tpPx": tp_px,
            "posTags": pos_tags,
            "why": why,
            "score": item["score"],
            "news": [{"src": n["src"], "title": n["title"], "url": n["url"], "date": n["date"]} for n in matched[:3]],
        })

    scored_us = final_us
    us_pullback = [x for x in scored_us if -2.0 <= x["dayChg"] <= 3.0 and x["score"] >= 60][:10]

    return {
        "updatedAt": now_kst.strftime("%Y-%m-%d %H:%M KST (미국 지수·월가 뉴스 100%)"),
        "regime": {
            "score": us_score,
            "type": u_type,
            "badge": u_badge,
            "title": u_title,
            "desc": f"미국 주요 {len(US_UNIVERSE)}종목과 실시간 월가 기사 {len(us_news)}건(호재 {pos_cnt} · 주의 {neg_cnt}), S&P500·나스닥·필라델피아 반도체·VIX를 종합 분석한 결과입니다.",
        },
        "indices": {
            "sp500": sp500,
            "nasdaq": nasdaq,
            "sox": sox,
            "vix": vix,
        },
        "picks": scored_us[:15],
        "pullback": us_pullback,
        "news": [{"src": n["src"], "title": n["title"], "url": n["url"], "date": n["date"]} for n in us_news[:40]],
    }


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

    us_section = build_us_section(now_kst)

    payload = {
        "updatedAt": now_kst.strftime("%Y-%m-%d %H:%M KST (국장·미장 뉴스 100%)"),
        "us": us_section,
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
