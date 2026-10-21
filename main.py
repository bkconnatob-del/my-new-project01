import time
import requests
import pandas as pd
import ta
import threading
from datetime import datetime
from fastapi import FastAPI

# =========================================================================
# 1. 환경 설정 및 텔레그램 / 키움증권 REST API 인증 정보
# =========================================================================
TOKEN = "8454731178:AAFVCPPT8hzThwE0JuTvE5UveNH79QRGnZ4"
CHAT_ID = "8987887618"

# 사용자님의 실제 키움 API 키 정보가 안전하게 고정되어 있습니다.
KIWOOM_APPKEY = "bCODahcHXXLWG-rfoHMJ12lDf1eqXUNMKEQlJ6EF-Oc"
KIWOOM_SECRET = "0TZGYURPv6OADRC35zwDy1FcylTJFoLsDa_sp1hPyAc"

LOOP_INTERVAL = 180 
IS_RUNNING = True  
LAST_UPDATE_ID = None  

# FastAPI 객체 생성 (Render 시스템이 바로 인식하도록 전역 배치)
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
        res = requests.post(url, json=payload, timeout=5)
        if res.status_code == 200:
            return res.json().get("access_token")
    except:
        pass
    return None

def fetch_kiwoom_stock_data(stock_code, access_token):
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {access_token}"}
    day_url = "https://kiwoom.com" 
    min_url = "https://kiwoom.com"
    tick_url = "https://kiwoom.com"
    
    try:
        res_day = requests.get(day_url, headers=headers, params={"stock_code": stock_code, "count": "65"}, timeout=5).json()
        day_list = res_day.get("output", [])[::-1]
        df_day = pd.DataFrame(day_list)
        df_day["close"] = df_day["close_price"].astype(int)
        df_day["volume"] = df_day["acc_volume"].astype(int)
        
        res_min = requests.get(min_url, headers=headers, params={"stock_code": stock_code, "minute_type": "5", "count": "50"}, timeout=5).json()
        min_list = res_min.get("output", [])[::-1]
        df_min = pd.DataFrame(min_list)
        df_min["close"] = df_min["close_price"].astype(float)
        df_min["low"] = df_min["low_price"].astype(float)
        
        summary_info = res_day.get("summary", {})
        current_price = int(summary_info.get("current_price", 0))
        market_cap = int(summary_info.get("market_cap_amount", 0)) 
        deal_value = int(summary_info.get("acc_deal_amount", 0)) 
        
        res_tick = requests.get(tick_url, headers=headers, params={"stock_code": stock_code}, timeout=5).json()
        tick_list = res_tick.get("output", [])
        tick_deals = []
        for t in tick_list:
            volume = int(t.get("tick_volume", 0))
            price = int(t.get("tick_price", 0))
            tick_deals.append(volume * price)
            
        return df_day, df_min, current_price, market_cap, deal_value, tick_deals
    except:
        pass
    return None

# =========================================================================
# 3. 텔레그램 명령 제어
# =========================================================================
def check_telegram_commands():
    global IS_RUNNING, LAST_UPDATE_ID
    url = f"https://api.telegram.org/bot{TOKEN}/getUpdates"
    
    params = {"timeout": 1}
    if LAST_UPDATE_ID is not None:
        params["offset"] = LAST_UPDATE_ID + 1
        
    try:
        res = requests.get(url, params=params, timeout=3).json()
        results = res.get("result", [])
        
        if not results:
            return

        for update in results:
            LAST_UPDATE_ID = update.get("update_id", 0)  
            message_obj = update.get("message", {})
            last_text = message_obj.get("text", "")
            
            if last_text == "/stop" and IS_RUNNING:
                IS_RUNNING = False
                requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json={"chat_id": CHAT_ID, "text": "🛑 [클라우드] 장중 자동 감시 엔진을 일시 정지합니다."}, timeout=3)
            elif last_text == "/start" and not IS_RUNNING:
                IS_RUNNING = True
                requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json={"chat_id": CHAT_ID, "text": "🚀 [클라우드] 장중 자동 감시 엔진을 다시 시작합니다."}, timeout=3)
    except: 
        pass

# =========================================================================
# 4. 핵심 전략 조건식 판별 엔진
# =========================================================================
def analyze_strategy_conditions(day_df, min_df, current_price, market_cap, deal_value, tick_deals):
    try:
        if market_cap < 3000: return False, None
        if deal_value < 100: return False, None

        day_df["MA20"] = day_df["close"].rolling(window=20).mean()
        day_df["MA60"] = day_df["close"].rolling(window=60).mean()
        latest_day = day_df.iloc[-1]   
        prev_day = day_df.iloc[-2]     
        
        base_price = prev_day["close"]
        current_rate = ((current_price - base_price) / base_price) * 100
        if current_rate <= -23.0: return False, None

        cond_5_trend = latest_day["MA20"] > latest_day["MA60"]
        cond_6_above_ma20 = current_price > latest_day["MA20"]
        cond_7_volume_explosion = latest_day["volume"] >= prev_day["volume"] * 1.5
        
        min_df["RSI"] = ta.momentum.rsi(close=min_df["close"], window=14)
        bb = ta.volatility.BollingerBands(close=min_df["close"], window=20, window_dev=2)
        min_df["BB_lower"] = bb.bollinger_lband()
        
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
        pass
    return False, None

# =========================================================================
# 5. 실전 백그라운드 주식 감시 루프
# =========================================================================
def stock_monitoring_loop():
    # 렌더 서버 배포 안정화 대기 시간 (20초)
    time.sleep(20)
    
    try:
        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json={"chat_id": CHAT_ID, "text": "<b>🟢 [클라우드] 주식 감시 마스터 서버가 최종 가동되었습니다.</b>\n관심종목 21개 실시간 모니터링 시스템 스캔 작동.", "parse_mode": "HTML"}, timeout=5)
    except: 
        pass
    
    real_target_pool = [
        {"code": "005930", "name": "삼성전자"}, {"code": "000660", "name": "SK하이닉스"},
        {"code": "005380", "name": "현대차"}, {"code": "004980", "name": "성신양회"},
        {"code": "010170", "name": "대한광통신"}, {"code": "011070", "name": "LG이노텍"},
        {"code": "015760", "name": "한국전력"}, {"code": "0161M0", "name": "네오사피엔스"},
        {"code": "018250", "name": "애경산업"}, {"code": "032640", "name": "LG유플러스"},
        {"code": "034020", "name": "두산에너빌리티"}, {"code": "041020", "name": "폴라리스AI"},
        {"code": "082740", "name": "한화엔진"}, {"code": "190510", "name": "나무가"},
        {"code": "199430", "name": "제일일렉트릭"}, {"code": "201490", "name": "미투온"},
        {"code": "252110", "name": "한국비앤씨"}, {"code": "263050", "name": "에이비엘바이오"},
        {"code": "373200", "name": "대모"}, {"code": "413040", "name": "엑스게이트"},
        {"code": "425420", "name": "나노팀"}
    ]
    
    while True:
        try:
            check_telegram_commands()  
            
            if IS_RUNNING:
                print("[스캐너] 지정 종목 실시간 수급 추적 진행 중...")
                token = get_kiwoom_access_token()
                
                if token:
                    for s in real_target_pool:
                        live_data = fetch_kiwoom_stock_data(s["code"], token)
                        if live_data:
                            day_df, min_df, cur_price, mkt_cap, deal_val, tick_deals = live_data
                            is_ok, tech = analyze_strategy_conditions(day_df, min_df, cur_price, mkt_cap, deal_val, tick_deals)
                            if is_ok:
                                s.update({"price": cur_price, "mkt_cap": mkt_cap, "deal_val": deal_val, **tech})
                                msg = f"<b>🚨 [실전 추천] 조건 포착</b>\n• 종목: {s['name']}\n• 현재가: {cur_price:,}원 ({tech['rate']})\n• RSI: {tech['rsi']} / BB: {tech['bb_touch']}\n• 이평: {tech['golden_cross']}\n• 최대체결: {tech['max_deal']}"
                                try:
                                    requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=3)
                                except:
                                    pass
                else:
