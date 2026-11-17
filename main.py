import os
import time
import threading
from datetime import datetime
import requests
import pandas as pd
import ta
import uvicorn
from fastapi import FastAPI

# =========================================================================
# 1. 환경 설정 (Render Environment Variables에서 자동 로드)
# =========================================================================
TOKEN = os.environ.get("TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
KIWOOM_APPKEY = os.environ.get("KIWOOM_APP_KEY") or os.environ.get("KIWOOM_APPKEY")
KIWOOM_SECRET = os.environ.get("KIWOOM_SECRET_KEY") or os.environ.get("KIWOOM_SECRET")

# 모의투자 테스트 시에는 "https://mockapi.kiwoom.com" 으로 변경
KIWOOM_BASE_URL = "https://api.kiwoom.com"

LOOP_INTERVAL = 60       # 전체 감시 주기 (120초 = 2분)
SUMMARY_INTERVAL = 1800   # 시초가 대비 전 종목 브리핑 주기 (1800초 = 30분)
ALERT_COOLDOWN = 1800     # 동일 종목 중복 알림 방지 쿨타임 (30분)
IS_RUNNING = True

app = FastAPI()

@app.get("/")
def health_check():
    return {"status": "running", "timestamp": datetime.now().isoformat()}

# =========================================================================
# 2. 키움증권 공식 REST API 연동 모듈
# =========================================================================
def get_kiwoom_access_token():
    url = f"{KIWOOM_BASE_URL}/oauth2/token"
    headers = {"Content-Type": "application/json;charset=UTF-8"}
    payload = {
        "grant_type": "client_credentials",
        "appkey": KIWOOM_APPKEY,
        "secretkey": KIWOOM_SECRET
    }
    try:
        res = requests.post(url, headers=headers, json=payload, timeout=10)
        data = res.json()
        token = data.get("token") or data.get("access_token")
        if not token:
            print(f"[토큰 발급 실패] 응답: {data}")
        return token
    except Exception as e:
        print(f"[토큰 요청 예외] {e}")
        return None

def parse_kiwoom_num(val):
    """키움 API 응답의 부호(+/-) 및 콤마가 포함된 문자열을 숫자로 안전 변환"""
    if val is None:
        return 0.0
    s = str(val).replace(",", "").strip()
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0

def get_top20_volume_codes(access_token):
    """당일 거래량 상위 20개 종목 코드 조회 (ka10030)"""
    url = f"{KIWOOM_BASE_URL}/api/dostk/rkinfo"
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "authorization": f"Bearer {access_token}",
        "api-id": "ka10030"
    }
    payload = {
        "mrkt_tp": "000",         # 000: 전체(코스피+코스닥)
        "sort_tp": "1",           # 1: 거래량순
        "mang_stk_incls": "0",    # 0: 관리종목 미포함
        "crd_tp": "0",
        "trde_qty_tp": "0",
        "pric_tp": "0",
        "trde_prica_tp": "0",
        "mrkt_open_tp": "0",
        "stex_tp": "1"
    }
    try:
        res = requests.post(url, headers=headers, json=payload, timeout=10).json()
        items = res.get("tdy_trde_qty_upper", []) or res.get("output", [])
        top_codes = set()
        for item in items[:20]:
            code = str(item.get("stk_cd", "")).replace("_AL", "").replace("_NX", "").strip()
            if code:
                top_codes.add(code)
        return top_codes
    except Exception as e:
        print(f"[거래상위 20 조회 오류] {e}")
        return set()

def fetch_stock_detail_and_rsi(stock_code, access_token):
    """주식기본정보(ka10001) + 일봉차트(ka10081) 조회 후 시세 및 RSI 계산"""
    base_headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "authorization": f"Bearer {access_token}"
    }
    
    # 1) 주식기본정보요청 (ka10001)
    info_url = f"{KIWOOM_BASE_URL}/api/dostk/stkinfo"
    info_headers = {**base_headers, "api-id": "ka10001"}
    
    # 2) 주식일봉차트조회요청 (ka10081)
    chart_url = f"{KIWOOM_BASE_URL}/api/dostk/chart"
    chart_headers = {**base_headers, "api-id": "ka10081"}
    today_str = datetime.now().strftime("%Y%m%d")
    
    try:
        res_info = requests.post(
            info_url, headers=info_headers, json={"stk_cd": stock_code}, timeout=10
        ).json()
        
        if str(res_info.get("return_code", "0")) != "0":
            print(f"[기본정보 오류 - {stock_code}] {res_info.get('return_msg')}")
            return None
            
        cur_price = abs(int(parse_kiwoom_num(res_info.get("cur_prc"))))
        open_price = abs(int(parse_kiwoom_num(res_info.get("open_pric"))))
        flu_rt = parse_kiwoom_num(res_info.get("flu_rt"))          # 전일 대비 등락률 (%)
        trde_prica = abs(parse_kiwoom_num(res_info.get("trde_prica"))) # 누적 거래대금 (백만원 단위)
        
        # 일봉 차트 조회하여 RSI(14) 계산
        res_chart = requests.post(
            chart_url,
            headers=chart_headers,
            json={"stk_cd": stock_code, "base_dt": today_str, "upd_stkpc_tp": "1"},
            timeout=10
        ).json()
        
        chart_list = res_chart.get("stk_dt_pole_chart_qry", []) or res_chart.get("output", [])
        rsi_val = None
        if len(chart_list) >= 20:
            # 최신 데이터가 앞에 오므로 과거->최신 순으로 역정렬
            closes = [abs(parse_kiwoom_num(row.get("cur_prc"))) for row in reversed(chart_list[:60])]
            series_close = pd.Series(closes)
            rsi_series = ta.momentum.rsi(close=series_close, window=14)
            if not pd.isna(rsi_series.iloc[-1]):
                rsi_val = round(float(rsi_series.iloc[-1]), 2)
                
        return {
            "cur_price": cur_price,
            "open_price": open_price,
            "flu_rt": flu_rt,
            "trde_prica_million": trde_prica,  # 백만원 단위 (300억 = 30,000 백만원)
            "rsi": rsi_val
        }
    except Exception as e:
        print(f"[종목 조회 예외 - {stock_code}] {e}")
        return None

# =========================================================================
# 3. 텔레그램 알림 및 명령어 수신
# =========================================================================
def send_telegram_message(text):
    if not TOKEN or not CHAT_ID:
        print("[경고] TOKEN 또는 CHAT_ID 환경변수가 설정되지 않았습니다.")
        return
    try:
        url = f"https://telegram.org{TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        print(f"[텔레그램 전송 오류] {e}")

def check_telegram_commands():
    global IS_RUNNING
    if not TOKEN:
        return
    url = f"https://telegram.org{TOKEN}/getUpdates"
    try:
        res = requests.get(url, params={"offset": -1, "timeout": 1}, timeout=5).json()
        results = res.get("result", [])
        if results:
            last_text = results[-1].get("message", {}).get("text", "").strip()
            if last_text == "/stop" and IS_RUNNING:
                IS_RUNNING = False
                send_telegram_message("🛑 [클라우드] 장중 자동 감시 엔진을 일시 정지합니다.")
            elif last_text == "/start" and not IS_RUNNING:
                IS_RUNNING = True
                send_telegram_message("🚀 [클라우드] 장중 자동 감시 엔진을 다시 시작합니다.")
    except Exception:
        pass

# =========================================================================
# 4. 신규 5대 OR 조건 판별 엔진
# =========================================================================
def evaluate_or_conditions(stock_code, data, top20_codes):
    """
    조건 (OR):
    1) 당일 +15% 이상 상승
    2) 당일 -24% 이상 하락 (등락률 <= -24.0%)
    3) 거래대금 300억 이상 (키움 trde_prica 백만원 단위 기준 >= 30,000)
    4) 당일 거래량 상위 20개 종목 포함
    5) 일봉 RSI <= 35
    """
    matched_reasons = []
    
    flu_rt = data["flu_rt"]
    trde_prica_m = data["trde_prica_million"]
    rsi = data["rsi"]
    
    if flu_rt >= 15.0:
        matched_reasons.append(f"급등({flu_rt:+.2f}%)")
    if flu_rt <= -24.0:
        matched_reasons.append(f"급락({flu_rt:+.2f}%)")
    if trde_prica_m >= 30000:
        trde_eok = trde_prica_m / 100.0
        matched_reasons.append(f"거래대금 {trde_eok:,.0f}억")
    if stock_code in top20_codes:
        matched_reasons.append("당일 거래상위 TOP 20")
    if rsi is not None and rsi <= 35.0:
        matched_reasons.append(f"RSI 과매도({rsi})")
        
    return (len(matched_reasons) > 0), matched_reasons

# =========================================================================
# 5. 실전 백그라운드 감시 루프
# =========================================================================
def stock_monitoring_loop():
    time.sleep(10)
    
    send_telegram_message(
        "<b>🟢 [클라우드] 키움 REST API 주식 감시 서버가 부팅되었습니다.</b>\n"
        "• 감시 조건(OR): +15%↑ / -24%↓ / 거래대금 300억↑ / 거래상위 20 / RSI≤35\n"
        "• 관심종목 시초가 대비 등락 브리핑: 30분 주기"
    )
    
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
    
    last_summary_time = 0
    alert_history = {}  # 종목별 마지막 조건 포착 알림 시각 기록
    
    while True:
        check_telegram_commands()
        if IS_RUNNING:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] 키움 REST API 스캔 시작...")
            token = get_kiwoom_access_token()
            
            if token:
                now_ts = time.time()
                need_summary = (now_ts - last_summary_time) >= SUMMARY_INTERVAL
                summary_lines = []
                
                # 당일 거래량 상위 20개 종목 코드 먼저 조회
                top20_codes = get_top20_volume_codes(token)
                time.sleep(0.3)
                
                for s in real_target_pool:
