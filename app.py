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
from matplotlib.gridspec import GridSpec
import streamlit as st

# ✨ 구글 제미나이 및 에러 처리 라이브러리
import google.generativeai as genai
from google.api_core.exceptions import ResourceExhausted

# ======================================================================
# 1. 기본 설정 및 전역 변수
# ======================================================================
st.set_page_config(page_title="나만의 HTS - Bloomberg Edition", layout="wide")

# 한글 폰트 설정
plt.rcParams['font.family'] = 'Malgun Gothic' if os.name == 'nt' else 'AppleGothic'
plt.rcParams['axes.unicode_minus'] = False

HTTP_HEADERS = {"User-Agent": "Mozilla/5.0"}
SESSION = requests.Session()
SESSION.headers.update(HTTP_HEADERS)

PORTFOLIO_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "portfolio.json")
SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")

# ======================================================================
# 2. 헬퍼 함수 정의 (무조건 UI보다 위에 있어야 함)
# ======================================================================
def load_portfolio():
    if os.path.exists(PORTFOLIO_FILE):
        try:
            with open(PORTFOLIO_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}
    return {}

def save_portfolio(data):
    with open(PORTFOLIO_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def load_settings():
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return {"tg_token": "", "tg_chat_id": "", "gemini_api_key": ""}
    return {"tg_token": "", "tg_chat_id": "", "gemini_api_key": ""}

def save_settings(data):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

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
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": text}, timeout=5)
    except: pass

# ======================================================================
# 3. 핵심 분석 엔진 (데이터 수집 및 AI 분석)
# ======================================================================
@st.cache_data(ttl=600)
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

        if cond_bottom:
            sig, extra, reason, buy_p, tgt_p = "BUY", "(🔥실전: 찐바닥 턴어라운드)", "RSI 침체권 이탈 반등", cur_price, int(latest["MA20"])
        elif cond_pullback:
            sig, extra, reason, buy_p, tgt_p = "BUY", "(⭐실전: 20일선 안전 눌림목)", "상승 추세 속 20일선 지지", min(cur_price, int(latest["MA20"])), int(latest["BB_Upper"])
        elif cond_early:
            sig, extra, reason, buy_p, tgt_p = "BUY", "(🚀실전: MACD 상승 초입)", "MACD 양수 전환", int(latest["MA5"]), int(cur_price * (1 + target_pct / 100))

        if sig != "BUY" and cur_price >= latest["BB_Upper"] * 0.98 and latest["RSI"] < 75:
            sig, extra, reason = "BUY", f"(🔥실전: 초급등! 단기 +{target_pct}% 목표)", "볼린저 상단 돌파 단타"
            buy_p, tgt_p = max(int(latest["MA5"]), int(cur_price * 0.97)), int(cur_price * (1 + target_pct / 100))

        if latest["RSI"] > 80:
            sig, extra, reason, tgt_p = "SELL", "(극과열 주의)", "RSI 극과열(>80)", int(latest["MA20"])
        elif sig != "BUY" and (latest["RSI"] > 75 or cur_price < latest["MA20"] * 0.95):
            sig, extra, reason, tgt_p = "SELL", "(과열/추세이탈)", "RSI 과열 또는 지지선 이탈", int(latest["MA20"])

        result.update({
            "Price": cur_price, "Signal": sig, "Buy_Price": buy_p, "Target_Price": tgt_p, 
            "RSI": round(latest["RSI"], 1), "Extra": extra, "Reason": reason,
            "MACD_Hist": round(latest["MACD_Hist"], 2),
            "MA20": int(latest["MA20"]),
            "BB_Upper": int(latest["BB_Upper"])
        })
        return result
    except:
        return result

@st.cache_data(ttl=300)
def get_supply_demand_data(market: str, investor: str):
    sosok = "0" if market == "KOSPI" else "1"
    url = "https://finance.naver.com/sise/sise_quant.naver"
    params = {"sosok": sosok}

    soup = fetch_soup(url, params=params)
    fund_data = []

    table = soup.find("table", class_="type_2")
    if not table: return pd.DataFrame()

    for tr in table.find_all("tr"):
        a_tag = tr.find("a")
        if not a_tag or "code=" not in a_tag.get("href", ""): continue
        name = a_tag.text.strip()
        code = normalize_code(a_tag['href'].split('code=')[-1].split('&')[0])
        tds = tr.find_all("td")

        if len(tds) < 7: continue

        cur_price = parse_int(tds[2].text)
        volume = parse_int(tds[5].text)
        trade_amount = parse_int(tds[6].text)

        if volume > 0:
            fund_data.append({
                "순위": len(fund_data) + 1,
                "종목명": name, "종목코드": code, "현재가": cur_price,
                "거래량(천주)": volume // 1000, "거래대금(백만원)": trade_amount,
            })
    return pd.DataFrame(fund_data)

@st.cache_data(ttl=600)
def ask_gemini_analyst_safe(name, price, rsi, macd_hist, ma20, bb_upper):
    if not st.session_state.settings.get("gemini_api_key"):
        return "⚠️ Gemini API Key가 없습니다. 좌측 사이드바에서 입력해주세요."

    model = genai.GenerativeModel('gemini-1.5-flash') 
    
    prompt = f"""
    당신은 냉철하고 전문적인 실전 주식 트레이더입니다. 
    아래 종목의 현재 기술적 지표 데이터를 바탕으로 초단기 매매 관점에서 분석해 주세요.
    
    [데이터]
    - 종목명: {name}
    - 현재가: {price:,}원
    - RSI: {rsi} 
    - MACD 히스토그램: {macd_hist}
    - 20일선: {ma20:,}원
    - 볼린저 상단: {bb_upper:,}원
    
    [요청 사항]
    1. 현재 기술적 위치 평가
    2. 단기 목표가 및 손절가
    3. 종합 의견 (강력 매수 / 분할 매수 / 관망 / 매도 중 택 1)
    """
    
    try:
        response = model.generate_content(prompt)
        return response.text
    except ResourceExhausted:
        return "⚠️ API 무료 호출 한도를 초과했습니다. 약 1분 정도 기다렸다가 다시 시도해 주세요."
    except Exception as e:
        return f"⚠️ AI 분석 중 오류가 발생했습니다: {str(e)}"

# ======================================================================
# 4. 세션 상태 초기화 (반드시 UI 구성 전에 실행)
# ======================================================================
if "portfolio" not in st.session_state:
    st.session_state.portfolio = load_portfolio()

if "settings" not in st.session_state:
    st.session_state.settings = load_settings()

# ======================================================================
# 5. Streamlit 웹 UI 구현
# ======================================================================
st.title("HTS")

with st.sidebar:
    st.header("⚙️ 봇 & AI 설정")
    
    tg_token = st.text_input("텔레그램 토큰", type="password", value=st.session_state.settings.get("tg_token", ""), key="tg_token_input")
    tg_chat_id = st.text_input("텔레그램 Chat ID", type="password", value=st.session_state.settings.get("tg_chat_id", ""), key="tg_chat_id_input")
    gemini_api_key = st.text_input("Gemini API Key", type="password", value=st.session_state.settings.get("gemini_api_key", ""), key="gemini_api_input")
    
    if (tg_token != st.session_state.settings.get("tg_token") or 
        tg_chat_id != st.session_state.settings.get("tg_chat_id") or
        gemini_api_key != st.session_state.settings.get("gemini_api_key")):
        
        st.session_state.settings["tg_token"] = tg_token
        st.session_state.settings["tg_chat_id"] = tg_chat_id
        st.session_state.settings["gemini_api_key"] = gemini_api_key
        save_settings(st.session_state.settings)
        
    if gemini_api_key:
        genai.configure(api_key=gemini_api_key)

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
        with st.spinner("시장 전체를 스캔 중입니다. 잠시만 기다려주세요..."):
            kospi_df = get_naver_top_100("KOSPI")
            kosdaq_df = get_naver_top_100("KOSDAQ")
            all_stocks = pd.concat([kospi_df, kosdaq_df]).head(100).reset_index(drop=True)

            alerts = []
            results = []

            progress_bar = st.progress(0)
            total = len(all_stocks)

            for i, (_, row) in enumerate(all_stocks.iterrows()):
                res = analyze_stock_advanced(row["코드"], row["종목명"], target_pct)
                if res["Signal"] == "BUY":
                    results.append(res)
                    alerts.append(f"📈 매수 [{res['Name']}] 현재:{res['Price']:,} | 추천:{res['Buy_Price']:,} 부근 | {res['Extra']}")
                progress_bar.progress((i + 1) / total)

            if results:
                st.success(f"총 {len(results)}개의 매수 타점을 발견했습니다!")
                for r in results:
                    st.info(f"**{r['Name']}** ({r['Code']}) - 현재가: {r['Price']:,}원\n\n"
                            f"✅ **{r['Extra']}**\n\n"
                            f"🛒 추천 매수가: **{r['Buy_Price']:,}원** 부근\n\n"
                            f"🎯 단기 목표가: **{r['Target_Price']:,}원** | 📊 RSI: {r['RSI']}\n\n"
                            f"💡 근거: {r['Reason']}")

                if st.session_state.settings.get("tg_token") and st.session_state.settings.get("tg_chat_id"):
                    send_telegram(st.session_state.settings["tg_token"], st.session_state.settings["tg_chat_id"], "🔔 웹 HTS 스캔 알림\n\n" + "\n\n".join(alerts[:10]))
            else:
                st.warning("현재 시장에서 매수 조건에 부합하는 종목이 없습니다. 관망하세요.")

with tab2:
    st.subheader("내 포트폴리오 타점 진단 & AI 심층 분석")
    if not st.session_state.portfolio:
        st.info("좌측 사이드바에서 포트폴리오를 먼저 등록해 주세요.")
    else:
        for code, data in st.session_state.portfolio.items():
            res = analyze_stock_advanced(code, data["name"], target_pct)
            avg_price = data["price"]
            cur_price = res["Price"]
            profit = ((cur_price - avg_price) / avg_price * 100) if avg_price > 0 else 0

            with st.expander(f"🔎 {data['name']} (수익률: {profit:+.2f}%)"):
                st.write(f"- 내 매수가: {avg_price:,}원 / 현재가: {cur_price:,}원")
                if res["Signal"] == "BUY":
                    st.success(f"추가 매수 유효! 추천가: {res['Buy_Price']:,}원 | 목표가: {res['Target_Price']:,}원")
                elif res["Signal"] == "SELL":
                    st.error(f"리스크 관리 필요! 기준가: {res['Target_Price']:,}원 이탈 시 정리 고려")
                else:
                    st.warning("현재는 뚜렷한 방향성이 없습니다. 보유/관망을 추천합니다.")
                st.caption(f"분석 근거: {res['Reason']} (RSI: {res['RSI']})")

                # 제미나이 AI 분석 버튼
                if st.button(f"🤖 제미나이에게 {data['name']} 심층 분석 맡기기", key=f"ai_{code}"):
                    with st.spinner("제미나이가 차트 데이터를 분석 중입니다..."):
                        ai_insight = ask_gemini_analyst_safe(
                            name=data["name"], 
                            price=res["Price"], 
                            rsi=res["RSI"], 
                            macd_hist=res["MACD_Hist"], 
                            ma20=res["MA20"], 
                            bb_upper=res["BB_Upper"]
                        )
                        st.markdown("### 🤖 Gemini AI 트레이더 분석 결과")
                        st.info(ai_insight)

with tab3:
    st.subheader("실시간 네이버 수급 데이터")
    st.caption("※ 거래량/거래대금 상위 종목 기준 (네이버 금융 sise_quant)")

    market_sel = st.radio("시장 선택", ["KOSPI", "KOSDAQ"], horizontal=True)
    st.info("💡 현재는 거래량 상위 종목을 표시합니다. 외국인/기관 순매수 데이터는 별도 API 연동이 필요합니다.")

    if st.button("수급 데이터 불러오기"):
        with st.spinner("네이버 금융에서 파싱 중..."):
            df_supply = get_supply_demand_data(market_sel, "")

            if not df_supply.empty:
                st.dataframe(df_supply.head(20), use_container_width=True, hide_index=True)
            else:
                st.error("데이터를 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.")