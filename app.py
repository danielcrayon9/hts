import os
import json
import re
import datetime
from datetime import timedelta
import time
import requests
import pandas as pd
import FinanceDataReader as fdr
from bs4 import BeautifulSoup
from ta.trend import MACD
from ta.momentum import RSIIndicator
from ta.volatility import BollingerBands
import matplotlib.pyplot as plt
import streamlit as st

# 🚨 변경점 1: 구버전 genai 대신 신버전 라이브러리 임포트
from google import genai
from google.genai import types

# 구글 시트 연동 라이브러리
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ======================================================================
# 1. 기본 설정 및 전역 변수
# ======================================================================
st.set_page_config(page_title="나만의 HTS - Bloomberg Edition", layout="wide")

plt.rcParams['font.family'] = 'Malgun Gothic' if os.name == 'nt' else 'AppleGothic'
plt.rcParams['axes.unicode_minus'] = False

HTTP_HEADERS = {"User-Agent": "Mozilla/5.0"}
SESSION = requests.Session()
SESSION.headers.update(HTTP_HEADERS)

# ======================================================================
# 2. 헬퍼 함수 정의
# ======================================================================
def get_gsheet_client():
    creds_dict = dict(st.secrets["gcp_service_account"])
    scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    return gspread.authorize(creds)

def load_portfolio():
    try:
        client = get_gsheet_client()
        sheet = client.open_by_key(st.secrets["sheet_id"]).sheet1
        records = sheet.get_all_records()
        
        portfolio = {}
        for row in records:
            if not str(row.get('종목코드')).strip(): continue
            code = normalize_code(str(row['종목코드']))
            portfolio[code] = {
                "name": str(row['종목명']),
                "price": parse_int(row['매수가'])
            }
        return portfolio
    except: return {}

def save_portfolio(data):
    try:
        client = get_gsheet_client()
        sheet = client.open_by_key(st.secrets["sheet_id"]).sheet1
        headers = ["종목코드", "종목명", "매수가"]
        rows = [headers]
        for code, info in data.items():
            rows.append([code, info["name"], info["price"]])
        sheet.clear()
        sheet.update(values=rows, range_name="A1")
    except Exception as e: st.error(f"구글 시트 저장 오류: {e}")

def normalize_code(code): return str(code).strip().split(".")[0].zfill(6)
def parse_int(value, default=0):
    if value is None: return default
    s = re.sub(r"[^\d\-]", "", str(value).strip().replace(",", "").replace("+", ""))
    return int(s) if s not in ("", "-") else default
def parse_float(value, default=0.0):
    if value is None: return default
    s = re.sub(r"[^\d\.\-\+]", "", str(value).strip().replace(",", "").replace("플러스", "+").replace("마이너스", "-"))
    return float(s) if s not in ("", "-", "+", ".", "-.", "+.") else default

def fetch_soup(url, params=None):
    res = SESSION.get(url, params=params, timeout=5)
    res.encoding = 'euc-kr'
    return BeautifulSoup(res.text, "html.parser")

def send_telegram(token, chat_id, text):
    if not token or not chat_id: return
    try: requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": text}, timeout=5)
    except: pass

def get_recent_news(code):
    url = f"https://finance.naver.com/item/news_news.naver?code={code}"
    soup = fetch_soup(url)
    news_list = []
    table = soup.find('table', {'class': 'type5'})
    if not table: return "최근 뉴스가 없습니다."
    for tr in table.find_all('tr'):
        title_td = tr.find('td', {'class': 'title'})
        if title_td and title_td.find('a'):
            news_list.append(title_td.find('a').text.strip())
        if len(news_list) >= 5: break
    return "\n".join([f"- {news}" for news in news_list]) if news_list else "최근 뉴스가 없습니다."

# ======================================================================
# 3. 핵심 분석 엔진 (데이터 수집 및 AI 분석)
# ======================================================================
def get_naver_top_100(market="KOSPI"):
    sosok = 0 if market == "KOSPI" else 1
    data = []
    for page in [1, 2]:
        soup = fetch_soup("https://finance.naver.com/sise/sise_market_sum.naver", params={"sosok": sosok, "page": page})
        table = soup.find("table", {"class": "type_2"})
        if not table: continue
        for row in table.find_all("tr"):
            if "onmouseover" not in row.attrs: continue
            cols = row.find_all("td")
            if len(cols) < 5 or not cols[1].find("a"): continue
            code = cols[1].find("a")["href"].split("code=")[-1].strip()
            data.append({
                "종목명": cols[1].text.strip(),
                "코드": normalize_code(code),
                "현재가": parse_int(cols[2].text),
                "등락률": parse_float(cols[4].text)
            })
    return pd.DataFrame(data)

def analyze_stock_advanced(ticker: str, name: str, target_pct: int = 5):
    ticker = normalize_code(ticker)
    result = {"Name": name, "Code": ticker, "Price": 0, "Signal": "HOLD", "Target_Price": 0, "Buy_Price": 0, "RSI": 0.0, "Extra": "", "Reason": "데이터 부족", "MACD_Hist": 0, "MA20": 0, "BB_Upper": 0}
    try:
        df = fdr.DataReader(ticker, datetime.datetime.today() - timedelta(days=365))
        if df.empty or len(df) < 60: return result

        df = df.sort_index()
        macd_obj = MACD(close=df["Close"])
        bb = BollingerBands(close=df["Close"])
        df["MACD"], df["MACD_Sig"], df["MACD_Hist"] = macd_obj.macd(), macd_obj.macd_signal(), macd_obj.macd_diff()
        df["RSI"] = RSIIndicator(close=df["Close"]).rsi()
        df["BB_Upper"], df["BB_Lower"], df["MA20"] = bb.bollinger_hband(), bb.bollinger_lband(), bb.bollinger_mavg()
        df["MA5"], df["MA60"] = df["Close"].rolling(5).mean(), df["Close"].rolling(60).mean()

        df = df.dropna()
        if len(df) < 2: return result

        latest, prev = df.iloc[-1], df.iloc[-2]
        cur_price = int(latest["Close"])

        is_uptrend = latest["MA20"] > latest["MA60"]
        cond_pullback = is_uptrend and (latest["MA20"] * 0.98 <= cur_price <= latest["MA20"] * 1.03) and (40 <= latest["RSI"] <= 60)
        cond_bottom = (prev["RSI"] < 35) and (latest["RSI"] > prev["RSI"]) and (cur_price > prev["Close"])
        cond_early = (prev["MACD_Hist"] < 0) and (latest["MACD_Hist"] >= 0) and (latest["RSI"] < 65)

        sig, extra, reason, buy_p, tgt_p = "HOLD", "", "시그널 없음", 0, 0

        if cond_bottom: sig, extra, reason, buy_p, tgt_p = "BUY", "(🔥찐바닥)", "RSI 침체권 이탈", cur_price, int(latest["MA20"])
        elif cond_pullback: sig, extra, reason, buy_p, tgt_p = "BUY", "(⭐눌림목)", "20일선 지지", min(cur_price, int(latest["MA20"])), int(latest["BB_Upper"])
        elif cond_early: sig, extra, reason, buy_p, tgt_p = "BUY", "(🚀MACD상승)", "MACD 양수전환", int(latest["MA5"]), int(cur_price * (1 + target_pct / 100))

        if sig != "BUY" and cur_price >= latest["BB_Upper"] * 0.98 and latest["RSI"] < 75:
            sig, extra, reason, buy_p, tgt_p = "BUY", f"(🔥단기 +{target_pct}%)", "볼린저 돌파", max(int(latest["MA5"]), int(cur_price * 0.97)), int(cur_price * (1 + target_pct / 100))

        if latest["RSI"] > 80: sig, extra, reason, tgt_p = "SELL", "(극과열)", "RSI > 80", int(latest["MA20"])
        elif sig != "BUY" and (latest["RSI"] > 75 or cur_price < latest["MA20"] * 0.95): sig, extra, reason, tgt_p = "SELL", "(과열/이탈)", "RSI>75 또는 20일선 이탈", int(latest["MA20"])

        result.update({"Price": cur_price, "Signal": sig, "Buy_Price": buy_p, "Target_Price": tgt_p, "RSI": round(latest["RSI"], 1), "Extra": extra, "Reason": reason, "MACD_Hist": round(latest["MACD_Hist"], 2), "MA20": int(latest["MA20"]), "BB_Upper": int(latest["BB_Upper"])})
        return result
    except: return result

@st.cache_data(ttl=300)
def get_supply_demand_data(market: str, investor: str):
    sosok = "0" if market == "KOSPI" else "1"
    url = "https://finance.naver.com/sise/sise_quant.naver"
    soup = fetch_soup(url, params={"sosok": sosok})
    fund_data = []
    table = soup.find("table", class_="type_2")
    if not table: return pd.DataFrame()

    for tr in table.find_all("tr"):
        a_tag = tr.find("a")
        if not a_tag or "code=" not in a_tag.get("href", ""): continue
        tds = tr.find_all("td")
        if len(tds) < 7: continue
        vol = parse_int(tds[5].text)
        if vol > 0:
            fund_data.append({"순위": len(fund_data) + 1, "종목명": a_tag.text.strip(), "종목코드": normalize_code(a_tag['href'].split('code=')[-1].split('&')[0]), "현재가": parse_int(tds[2].text), "거래량(천주)": vol // 1000, "거래대금(백만원)": parse_int(tds[6].text)})
    return pd.DataFrame(fund_data)

@st.cache_data(ttl=600)
def ask_gemini_analyst_safe(name, price, rsi, macd_hist, ma20, bb_upper, news_data):
    api_key = st.secrets.get("gemini_api_key")
    if not api_key:
        return "⚠️ Streamlit Secrets에 Gemini API Key가 설정되지 않았습니다."

    prompt = f"""
    당신은 냉철하고 전문적인 실전 주식 트레이더입니다. 
    아래 종목의 현재 기술적 지표와 '최근 뉴스 헤드라인'을 종합하여 초단기 매매 관점에서 분석해 주세요.
    
    [데이터]
    - 종목명: {name}
    - 현재가: {price:,}원
    - RSI: {rsi} 
    - MACD 히스토그램: {macd_hist}
    - 20일선: {ma20:,}원
    - 볼린저 상단: {bb_upper:,}원
    
    [최근 핵심 뉴스 (호재/악재 판별용)]
    {news_data}
    
    [요청 사항]
    1. 최근 뉴스 헤드라인을 바탕으로 한 주가 모멘텀 분석
    2. 기술적 지표와 뉴스를 종합한 현재 위치 평가
    3. 단기 목표가 및 손절가
    4. 종합 의견 (강력 매수 / 분할 매수 / 관망 / 매도 중 택 1)
    """
    
    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model='gemini-2.5-flash',  # ✅ 수정: 1.5-flash → 2.0-flash
            contents=prompt
        )
        return response.text
    except Exception as e:
        return f"⚠️ AI 분석 중 오류가 발생했습니다: {str(e)}"

# ======================================================================
# 4. 세션 상태 초기화 및 UI
# ======================================================================
if "portfolio" not in st.session_state: st.session_state.portfolio = load_portfolio()

st.title("HTS")

with st.sidebar:
    st.header("⚙️ 봇 & AI 설정")
    st.info("✅ 설정값 및 포트폴리오가 구글 시트 DB에 연동되었습니다.")
    target_pct = st.selectbox("단타 목표 수익률", [5, 10, 15, 20], index=0)

    st.markdown("---")
    st.header("💼 내 포트폴리오 관리")
    p_name = st.text_input("종목명 (예: 삼성전자)")
    p_code = st.text_input("종목코드 (예: 005930)")
    p_price = st.number_input("나의 매수가", min_value=0, step=100)

    if st.button("포트폴리오 추가"):
        if p_name and p_code and p_price > 0:
            st.session_state.portfolio[normalize_code(p_code)] = {"name": p_name, "price": p_price}
            save_portfolio(st.session_state.portfolio)
            st.success(f"{p_name} 추가 완료!")

    st.write("현재 등록된 종목:")
    for code, data in list(st.session_state.portfolio.items()):
        col1, col2 = st.columns([4, 1])
        col1.caption(f"- {data['name']} ({code}): {data['price']:,}원")
        if col2.button("❌", key=f"del_{code}"):
            del st.session_state.portfolio[code]
            save_portfolio(st.session_state.portfolio)
            st.rerun()

tab1, tab2, tab3 = st.tabs(["🚀 VVIP 매수 스캔", "📈 내 포트폴리오 진단", "🔥 실시간 수급 현황"])

with tab1:
    st.subheader(f"인공지능 실시간 타점 스캔 (단타 목표: +{target_pct}%)")
    if st.button("⚡ 스캔 시작", type="primary"):
        with st.spinner("시장 스캔 중..."):
            all_stocks = pd.concat([get_naver_top_100("KOSPI"), get_naver_top_100("KOSDAQ")]).head(100).reset_index(drop=True)
            alerts, results = [], []
            pbar = st.progress(0)
            for i, (_, row) in enumerate(all_stocks.iterrows()):
                res = analyze_stock_advanced(row["코드"], row["종목명"], target_pct)
                if res["Signal"] == "BUY":
                    results.append(res)
                    alerts.append(f"📈 매수 [{res['Name']}] 현재:{res['Price']:,} | 추천:{res['Buy_Price']:,} 부근 | {res['Extra']}")
                pbar.progress((i + 1) / len(all_stocks))
            if results:
                st.success(f"총 {len(results)}개 매수 타점 발견!")
                for r in results: st.info(f"**{r['Name']}** - 현재가: {r['Price']:,}원\n\n✅ **{r['Extra']}**\n\n🎯 목표가: **{r['Target_Price']:,}원** | 💡 {r['Reason']}")
                t_tok, t_id = st.secrets.get("tg_token"), st.secrets.get("tg_chat_id")
                if t_tok and t_id: send_telegram(t_tok, t_id, "🔔 스캔 알림\n" + "\n".join(alerts[:10]))
            else: st.warning("매수 조건에 부합하는 종목이 없습니다.")

with tab2:
    st.subheader("내 포트폴리오 타점 진단 & AI 심층 분석")
    if not st.session_state.portfolio: st.info("사이드바에서 포트폴리오를 등록해주세요.")
    else:
        for code, data in st.session_state.portfolio.items():
            res = analyze_stock_advanced(code, data["name"], target_pct)
            profit = ((res["Price"] - data["price"]) / data["price"] * 100) if data["price"] > 0 else 0
            with st.expander(f"🔎 {data['name']} (수익률: {profit:+.2f}%)"):
                st.write(f"- 내 매수가: {data['price']:,}원 / 현재가: {res['Price']:,}원")
                if res["Signal"] == "BUY": st.success(f"추가 매수 유효! 추천가: {res['Buy_Price']:,}원 | 목표가: {res['Target_Price']:,}원")
                elif res["Signal"] == "SELL": st.error(f"리스크 관리 필요! 기준가: {res['Target_Price']:,}원 이탈 시 정리 고려")
                else: st.warning("현재는 뚜렷한 방향성이 없습니다. 보유/관망 추천.")
                
                if st.button(f"🤖 제미나이 심층 분석", key=f"ai_{code}"):
                    with st.spinner("분석 중..."):
                        news = get_recent_news(code)
                        ai_insight = ask_gemini_analyst_safe(data["name"], res["Price"], res["RSI"], res["MACD_Hist"], res["MA20"], res["BB_Upper"], news)
                        st.markdown("### 🤖 분석 결과")
                        with st.expander("📰 최근 뉴스"): st.write(news)
                        st.info(ai_insight)

with tab3:
    st.subheader("실시간 수급 데이터")
    market_sel = st.radio("시장 선택", ["KOSPI", "KOSDAQ"], horizontal=True)
    if st.button("수급 데이터 불러오기"):
        with st.spinner("파싱 중..."):
            df_supply = get_supply_demand_data(market_sel, "")
            if not df_supply.empty: st.dataframe(df_supply.head(20), use_container_width=True, hide_index=True)
            else: st.error("데이터 로드 실패.")