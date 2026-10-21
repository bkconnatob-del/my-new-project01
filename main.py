import time
import requests
import pandas as pd
import ta
import threading
from datetime import datetime
from fastapi import FastAPI
import uvicorn

# =========================================================================
# 1. 환경 설정 및 텔레그램 / 키움증권 REST API 인증 정보
# =========================================================================
TOKEN = "8454731178:AAFVCPPT8hzThwE0JuTvE5UveNH79QRGnZ4"
CHAT_ID = "8987887618"

# 🌟 중요: 장 가동 전에 본인의 실제 키움 API 키로 변경해 주세요!
KIWOOM_APPKEY = "bC0DahcHKXLwG-rfoHMJI2lDf1eqXUNKMEQ1J6EF-Oc"
KIWOOM_SECRET = "0Tz6YURPv6OADRC35zwDyiFcylTJEoLsDa_sp1hPyAc"

LOOP_INTERVAL = 180 
IS_RUNNING = True  # 폰 제어용 플래그

# FastAPI 객체 생성 (Render 배포 실패 방지용 가짜 웹 서버 신호기)
app = FastAPI()

@app.get("/")
def read_root():
    return {"status": "healthy", "bot_running": IS_RUNNING}

# =========================================================================
# 2. 키움증권 REST API 데이터 수집 모듈
# =========================================================================
def get_kiwoom_access_token():
    url = "https://kiwoom.com"
    payload = {"grant_type": "client_credentials", "appkey": KIWOOM_APPKEY, "secretkey": KIWOOM_SECRET}
    try:
        res = requests.post(url, json=payload)
        return res.json().get("access_token")
    except:
        return None

def fetch_kiwoom_stock_data(stock_code, access_token):
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {access_token}"}
    
    day_url = "https://kiwoom.com" 
    min_url = "https://kiwoom.com"
    tick_url = "https://kiwoom.com"
    
    try:
        # 일봉 데이터 파싱
        res_day = requests.get(day_url, headers=headers, params={"stock_code": stock_code, "count": "65"}).json()
        day_list = res_day.get("output", [])[::-1]
        df_day = pd.DataFrame(day_list)
        df_day["close"] = df_day["close_price"].astype(int)
        df_day["volume"] = df_day["acc_volume"].astype(int)
        
        # 5분봉 데이터 파싱
        res_min = requests.get(min_url, headers=headers, params={"stock_code": stock_code, "minute_type": "5", "count": "50"}).json()
        min_list = res_min.get("output", [])[::-1]
        df_min = pd.DataFrame(min_list)
        df_min["close"] = df_min["close_price"].astype(float)
        df_min["low"] = df_min["low_price"].astype(float)
        
        # 현재가 및 재무 요약 데이터 가공
        summary_info = res_day.get("summary", {})
        current_price = int(summary_info.get("current_price", 0))
        market_cap = int(summary_info.get("market_cap_amount", 0)) 
        deal_value = int(summary_info.get("acc_deal_amount", 0)) 
        
        # 건당 1천만 원 수급 분석용 틱 데이터 추출
        res_tick = requests.get(tick_url, headers=headers, params={"stock_code": stock_code}).json()
        tick_list = res_tick.get("output", [])
        tick_deals = []
        for t in tick_list:
            volume = int(t.get("tick_volume", 0))
            price = int(t.get("tick_price", 0))
            tick_deals.append(volume * price)
            
        return df_day, df_min, current_price, market_cap, deal_value, tick_deals
    except:
        return None

def check_telegram_commands():
    global IS_RUNNING
    url = f"https://api.telegram.org/bot{TOKEN}/getUpdates"
    try:
        res = requests.get(url, params={"offset": -1, "timeout": 1}).json()
        results = res.get("result", [])
        if results:
            last_text = results[-1].get("message", {}).get("text", "")
            if last_text == "/stop" and IS_RUNNING:
                IS_RUNNING = False
                requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json={"chat_id": CHAT_ID, "text": "🛑 [클라우드] 장중 자동 감시 엔진을 일시 정지합니다."})
            elif last_text == "/start" and not IS_RUNNING:
                IS_RUNNING = True
                requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json={"chat_id": CHAT_ID, "text": "🚀 [클라우드] 장중 자동 감시 엔진을 다시 시작합니다."})
    except: pass

# =========================================================================
# 3. 핵심 전략 조건식 판별 엔진
# =========================================================================
def analyze_strategy_conditions(day_df, min_df, current_price, market_cap, deal_value, tick_deals):
    try:
        if market_cap < 3000: return False, None
        if deal_value < 100: return False, None

        day_df["MA20"] = day_df["close"].rolling(window=20).mean()
        day_df["MA60"] = day_df["close"].rolling(window=60).mean()
        latest_day = day_df.iloc[-1]   
        prev_day = day_df.iloc[-2]     
        
        # [조건 1] 낙주 차단 하한선 제한 (-23% 이하 종목 필터링 탈락)
        base_price = prev_day["close"]
        current_rate = ((current_price - base_price) / base_price) * 100
        if current_rate <= -23.0: return False, None

        cond_5_trend = latest_day["MA20"] > latest_day["MA60"]
        cond_6_above_ma20 = current_price > latest_day["MA20"]
        cond_7_volume_explosion = latest_day["volume"] >= prev_day["volume"] * 1.5
        
        min_df["RSI"] = ta.momentum.rsi(close=min_df["close"], window=14)
        bb = ta.volatility.BollingerBands(close=min_df["close"], window=20, window_dev=2)
        min_df["BB_lower"] = bb.bollinger_lband()
        
        # [조건 2] 5분봉 기준 이동평균선 돌파(골든크로스) 판별
        min_df["MA5"] = min_df["close"].rolling(window=5).mean()
        min_df["MA20"] = min_df["close"].rolling(window=20).mean()
        min_df["MA60"] = min_df["close"].rolling(window=60).mean()
        latest_min = min_df.iloc[-1]   
        prev_min = min_df.iloc[-2]
        
        gc_20_60 = (latest_min["MA20"] >= latest_min["MA60"]) and (prev_min["MA20"] <= prev_min["MA60"])
        gc_5_20 = (latest_min["MA5"] >= latest_min["MA20"]) and (prev_min["MA5"] <= prev_min["MA20"])
        cond_min_golden_cross = gc_20_60 or gc_5_20

        cond_rsi_oversold = latest_min["RSI"] < 30
        cond_bb_touch = latest_min["low"] <= latest_min["BB_lower"]
        cond_intraday_dip = cond_rsi_oversold or cond_bb_touch
        
        # [조건 3] 한 호가(체결)당 1,000만 원 이상 수급 실시간 감지
        has_big_buyer = any(deal_amount >= 10000000 for deal_amount in tick_deals)

        is_matched = (cond_5_trend and cond_6_above_ma20 and cond_7_volume_explosion) and \
                     (cond_intraday_dip or cond_min_golden_cross) and \
                     has_big_buyer
        
        gc_type = "20-60선 크로스" if gc_20_60 else ("5-20선 크로스" if gc_5_20 else "미발생")
        
        return is_matched, {
            "rate": f"{current_rate:+.2f}%",
            "rsi": round(latest_min["RSI"], 1),
            "bb_touch": "터치" if cond_bb_touch else "미터치",
            "golden_cross": gc_type,
            "max_deal": f"{max(tick_deals)/10000:,.0f}만원" if tick_deals else "0원"
        }
    except:
        return False, None

# =========================================================================
# 4. 실전 백그라운드 스케줄러 가동 루프 부위
# =========================================================================
def stock_monitoring_loop():
    requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json={"chat_id": CHAT_ID, "text": "<b>🟢 [클라우드] 주식 감시 마스터 서버가 성공적으로 켜졌습니다.</b>\n요청하신 21개 실전 관심종목 감시 대기 중입니다.", "parse_mode": "HTML"})
    
    # 🌟 요청하신 관심종목 21개 일반 주식 풀 탑재 완료
    real_target_pool = [
        {"code": "005930", "name": "삼성전자"},
        {"code": "000660", "name": "SK하이닉스"},
        {"code": "005380", "name": "현대차"},
        {"code": "004980", "name": "성신양회"},
        {"code": "010170", "name": "대한광통신"},
        {"code": "011070", "name": "LG이노텍"},
        {"code": "015760", "name": "한국전력"},
        {"code": "0161M0", "name": "네오사피엔스"},
        {"code": "018250", "name": "애경산업"},
        {"code": "032640", "name": "LG유플러스"},
        {"code": "034020", "name": "두산에너빌리티"},
        {"code": "041020", "name": "폴라리스AI"},
        {"code": "082740", "name": "한화엔진"},
        {"code": "190510", "name": "나무가"},
        {"code": "199430", "name": "제일일렉트릭"},
        {"code": "201490", "name": "미투온"},
        {"code": "252110", "name": "한국비앤씨"},
        {"code": "263050", "name": "에이비엘바이오"},
        {"code": "373200", "name": "대모"},
        {"code": "413040", "name": "엑스게이트"},
        {"code": "425420", "name": "나노팀"}
    ]
    
    while True:
        check_telegram_commands()  # 모바일 리모컨 수신 체크
        
        if IS_RUNNING:
            current_time = datetime.now().strftime("%H:%M:%S")
            print(f"[{current_time}] 지정 종목군 키움 실시간 시세 조회 및 전수 검색 중...")
            
            token = get_kiwoom_access_token()
            matched_list = []
            
            if token:
                for s in real_target_pool:
                    live_data = fetch_kiwoom_stock_data(s["code"], token)
                    if live_data:
                        day_df, min_df, cur_price, mkt_cap, deal_val, tick_deals = live_data
                        is_ok, tech = analyze_strategy_conditions(day_df, min_df, cur_price, mkt_cap, deal_val, tick_deals)
                        if is_ok:
                            s.update({"price": cur_price, "mkt_cap": mkt_cap, "deal_val": deal_val, **tech})
                            matched_list.append(s)
                
                # 조건 일치 종목 실시간 알림 팝업 발송
                if matched_list:
                    msg = f"<b>🚨 [실전 추천] 수급 집중 우상향 눌림·돌파 포착</b>\n"
                    msg += f"⏱️ 포착 시간: {datetime.now().strftime('%H:%M:%S')}\n\n"
                    for i, stock in enumerate(matched_list, 1):
                        msg += f"{i}. <b>{stock['name']}</b> ({stock['code']})\n"
                        msg += f"   • 현재가: {stock['price']:,}원 ({stock['rate']})\n"
                        msg += f"   • 분봉 지표: RSI {stock['rsi']} / BB하단: {stock['bb_touch']}\n"
                        msg += f"   • 분봉 이평: ⚡ <b>{stock['golden_cross']}</b>\n"
                        msg += f"   • 순간 수급: 💰 건별 최대 체결 <code>{stock['max_deal']}</code>\n\n"
