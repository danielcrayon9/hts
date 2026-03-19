import time
import streamlit as st
import google.generativeai as genai
from google.api_core.exceptions import ResourceExhausted

# 🚨 [핵심 해결 방법] 사이드바를 그리기 전에, 반드시 세션 상태를 '먼저' 만들어 줍니다.
if "portfolio" not in st.session_state:
    st.session_state.portfolio = load_portfolio()

if "settings" not in st.session_state:
    st.session_state.settings = load_settings()


# ======================================================================
# 1. 사이드바 설정에 Gemini API Key 추가
# ======================================================================
with st.sidebar:
    st.header("⚙️ 봇 & AI 설정")
    
    # ✨ 제미나이 API 설정 추가
    gemini_api_key = st.text_input("Gemini API Key", type="password", 
                                   value=st.session_state.settings.get("gemini_api_key", ""),
                                   key="gemini_api_input")
    
    if gemini_api_key != st.session_state.settings.get("gemini_api_key"):
        st.session_state.settings["gemini_api_key"] = gemini_api_key
        save_settings(st.session_state.settings)
        
    if gemini_api_key:
        genai.configure(api_key=gemini_api_key) # API 키 세팅


# ======================================================================
# 2. 제미나이에게 타점 분석을 요청하는 헬퍼 함수
# ======================================================================
# 1. Streamlit 캐싱 적용: 동일한 파라미터가 들어오면 10분(600초) 동안은 API를 재호출하지 않음
@st.cache_data(ttl=600)
def ask_gemini_analyst_safe(name, price, rsi, macd_hist, ma20, bb_upper):
    if not st.session_state.settings.get("gemini_api_key"):
        return "⚠️ Gemini API Key가 없습니다. 좌측 사이드바에서 입력해주세요."

    # 2. 모델 변경: 무료 한도가 넉넉한 Flash 모델 사용 (1분당 15회 가능)
    # 만약 꼭 Pro를 쓰고 싶다면 'gemini-1.5-pro'로 변경하시되 연속 클릭에 주의해야 합니다.
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
    
    # 3. 예외 처리 로직 (try-except)
    try:
        response = model.generate_content(prompt)
        return response.text
        
    except ResourceExhausted:
        # 1분당 호출 횟수(RPM)를 초과했을 때 앱이 뻗지 않고 안내 메시지만 출력
        return "⚠️ API 무료 호출 한도를 초과했습니다. 약 1분 정도 기다렸다가 다시 시도해 주세요."
        
    except Exception as e:
        # 기타 예상치 못한 네트워크 에러 등 방어
        return f"⚠️ AI 분석 중 오류가 발생했습니다: {str(e)}"


# ======================================================================
# 3. 기존 analyze_stock_advanced 함수에 제미나이 연동
# ======================================================================
# ... (기존 지표 계산 로직은 그대로 사용) ...

        if cond_bottom:
            sig, extra, reason, buy_p, tgt_p = "BUY", "(🔥실전: 찐바닥 턴어라운드)", "RSI 침체권 이탈 반등", cur_price, int(latest["MA20"])
        # ... (기존 로직 유지) ...

        result.update({
            "Price": cur_price, "Signal": sig, "Buy_Price": buy_p, "Target_Price": tgt_p, 
            "RSI": round(latest["RSI"], 1), "Extra": extra, "Reason": reason,
            # 제미나이에게 넘겨주기 위해 원시 데이터 일부 저장
            "MACD_Hist": round(latest["MACD_Hist"], 2),
            "MA20": int(latest["MA20"]),
            "BB_Upper": int(latest["BB_Upper"])
        })
        return result

# ======================================================================
# 4. 스트림릿 화면 출력부 (tab2 내 포트폴리오 진단 쪽에 추가)
# ======================================================================
with tab2:
    st.subheader("내 포트폴리오 타점 진단 & AI 심층 분석")
    if not st.session_state.portfolio:
        st.info("좌측 사이드바에서 포트폴리오를 먼저 등록해 주세요.")
    else:
        for code, data in st.session_state.portfolio.items():
            res = analyze_stock_advanced(code, data["name"], target_pct)
            
            with st.expander(f"🔎 {data['name']} (현재가: {res['Price']:,}원)"):
                st.write(f"기본 알고리즘 진단: **{res['Signal']}** / 기준 근거: {res['Reason']}")
                
                # 버튼을 누를 때만 API를 호출하도록 설계 (비용/속도 절감)
                if st.button(f"🤖 제미나이에게 {data['name']} 심층 분석 맡기기", key=f"ai_{code}"):
                    with st.spinner("제미나이가 차트 데이터를 분석 중입니다..."):
                        
                        # 💡 여기 함수 이름을 위에서 정의한 '_safe'가 붙은 이름으로 맞춰줍니다!
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