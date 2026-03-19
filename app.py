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

import google.generativeai as genai
from google.api_core.exceptions import ResourceExhausted

# ✨ 구글 시트 연동 라이브러리 추가
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

gemini_api_key = st.secrets.get("gemini_api_key", "")
if gemini_api_key:
    genai.configure(api_key=gemini_api_key)

# ======================================================================
# 2. 헬퍼 함수 정의 (✨ 구글 시트 DB 연동으로 완전 개편)
# ======================================================================
def get_gsheet_client():
    """st.secrets의 정보를 바탕으로 구글 시트 API 인증을 수행합니다."""
    creds_dict = dict(st.secrets["gcp_service_account"])
    scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    return gspread.authorize(creds)

def load_portfolio():
    """구글 시트에서 포트폴리오 데이터를 읽어옵니다."""
    try:
        client = get_gsheet_client()
        sheet = client.open_by_key(st.secrets["sheet_id"]).sheet1
        records = sheet.get_all_records()
        
        portfolio = {}
        for row in records:
            if not str(row.get('종목코드')).strip():
                continue
            code = normalize_code(str(row['종목코드']))
            portfolio[code] = {
                "name": str(row['종목명']),
                "price": parse_int(row['매수가'])
            }
        return portfolio
    except Exception as e:
        # 최초 연동 시 시트가 비어있거나 에러가 날 경우 빈 딕셔너리 반환
        return {}

def save_portfolio(data):
    """현재 포트폴리오 데이터를 구글 시트에 통째로 덮어씁니다."""
    try:
        client = get_gsheet_client()
        sheet = client.open_by_key(st.secrets["sheet_id"]).sheet1
        
        # 헤더 설정
        headers = ["종목코드", "종목명", "매수가"]
        rows = [headers]
        
        for code, info in data.items():
            rows.append([code, info["name"], info["price"]])
            
        # 기존 데이터 초기화 후 새로운 데이터 밀어넣기
        sheet.clear()
        sheet.update(values=rows, range_name="A1")
    except Exception as e:
        st.error(f"구글 시트 저장 중 오류가 발생했습니다: {e}")

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
# (기존 fetch_soup, send_telegram 함수 아래에 추가)

def get_recent_news(code):
    """네이버 금융에서 해당 종목의 최신 뉴스 헤드라인 5개를 가져옵니다."""
    url = f"https://finance.naver.com/item/news_news.naver?code={code}"
    soup = fetch_soup(url)
    news_list = []
    
    table = soup.find('table', {'class': 'type5'})
    if not table: return "최근 뉴스가 없습니다."
    
    for tr in table.find_all('tr'):
        title_td = tr.find('td', {'class': 'title'})
        if title_td and title_td.find('a'):
            title = title_td.find('a').text.strip()
            news_list.append(title)
        if len(news_list) >= 5: # 상위 5개만 추출
            break
            
    return "\n".join([f"- {news}" for news in news_list]) if news_list else "최근 뉴스가 없습니다."
    
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
def ask_gemini_analyst_safe(name, price, rsi, macd_hist, ma20, bb_upper, news_data):
    if not st.secrets.get("gemini_api_key"):
        return "⚠️ Streamlit Secrets에 Gemini API Key가 설정되지 않았습니다."

    # 💡 404 에러 해결: 가장 안정적인 기본 모델명으로 변경했습니다.
    model = genai.GenerativeModel('gemini-1.5-flash') 
    
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
    1. 최근 뉴스 헤드라인을 바탕으로 한 주가 모멘텀(호재/악재) 분석
    2. 기술적 지표와 뉴스를 종합한 현재 위치 평가
    3. 단기 목표가 및 손절가
    4. 종합 의견 (강력 매수 / 분할 매수 / 관망 / 매도 중 택 1)
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

# ======================================================================
# 5. Streamlit 웹 UI 구현
# ======================================================================
st.title("HTS")

with st.sidebar:
    st.header("⚙️ 봇 & AI 설정")
    st.info("✅ 설정값 및 포트폴리오가 구글 시트 DB에 안전하게 연동되었습니다.")
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
            st.success(f"{p_name} 추가 완료! (구글 시트 저장됨)")

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

                tg_token = st.secrets.get("tg_token", "")
                tg_chat_id = st.secrets.get("tg_chat_id", "")
                if tg_token and tg_chat_id:
                    send_telegram(tg_token, tg_chat_id, "🔔 웹 HTS 스캔 알림\n\n" + "\n\n".join(alerts[:10]))
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

                # (기존 탭 2의 버튼 부분 코드 대체)
                if st.button(f"🤖 제미나이에게 {data['name']} 심층 분석 맡기기", key=f"ai_{code}"):
                    with st.spinner(f"실시간 뉴스 수집 및 AI 데이터 분석 중입니다..."):
                        
                        # 💡 뉴스 데이터 먼저 크롤링
                        recent_news = get_recent_news(code)
                        
                        # 💡 AI에게 지표 + 뉴스 데이터 함께 전달
                        ai_insight = ask_gemini_analyst_safe(
                            name=data["name"], 
                            price=res["Price"], 
                            rsi=res["RSI"], 
                            macd_hist=res["MACD_Hist"], 
                            ma20=res["MA20"], 
                            bb_upper=res["BB_Upper"],
                            news_data=recent_news  # 뉴스 데이터 추가!
                        )
                        st.markdown("### 🤖 Gemini AI 트레이더 종합 분석")
                        with st.expander("📰 참고한 최근 실시간 뉴스 보기"):
                            st.write(recent_news)
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