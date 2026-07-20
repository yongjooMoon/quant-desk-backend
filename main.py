import os
import json
import threading
import urllib.request
import urllib.parse
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import StreamingResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from supabase import create_client, Client
from pydantic import BaseModel
from dotenv import load_dotenv
import uvicorn
import FinanceDataReader as fdr
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import asyncio
from typing import Optional, List
from cachetools import TTLCache, cached
from apscheduler.schedulers.background import BackgroundScheduler
import pandas as pd

from cryptography.fernet import Fernet

# 기존 quant_core 및 real_estate 모듈 활용 (decrypt_text 제거, 기존 방식 복구)
from quant_core import load_price_from_db, fetch_naver_fundamental, calc_quant_metrics, now_kst, load_fundamental_from_db, save_fundamental_to_db
from real_estate import generate_excel_data

load_dotenv()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

ENCRYPTION_KEY = os.environ.get("ENCRYPTION_KEY")
if ENCRYPTION_KEY:
    cipher_suite = Fernet(ENCRYPTION_KEY.encode())
else:
    print("⚠️ 경고: ENCRYPTION_KEY 환경 변수가 설정되지 않았습니다.")
    cipher_suite = None

if not SUPABASE_URL or not SUPABASE_KEY:
    print("⚠️ 경고: Supabase 환경 변수가 설정되지 않았습니다.")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL else None

KST = ZoneInfo("Asia/Seoul")

# ==============================================================================
# 📊 프론트엔드에서 쏴주는 데이터를 받는 로깅 전용 API
# ==============================================================================
class VisitLogRequest(BaseModel):
    referer: str
    user_agent: str
    screen_id: str

def get_geolocation_from_ip(ip: str):
    try:
        if ip in ["127.0.0.1", "localhost", "::1"]:
            return "Local", "Local"
            
        url = f"http://ip-api.com/json/{ip}"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=2) as response:
            data = json.loads(response.read().decode('utf-8'))
            if data.get('status') == 'success':
                return data.get('country', 'Unknown'), data.get('city', 'Unknown')
    except Exception:
        pass
    return "Unknown", "Unknown"

def _process_log_visit(client_ip: str, payload: VisitLogRequest):
    if not supabase: return
    country, city = get_geolocation_from_ip(client_ip)
    
    try:
        kst_time = now_kst().strftime("%Y-%m-%d %H:%M:%S")
        ua_lower = payload.user_agent.lower()
        final_referer = payload.referer
        
        if "remember" in ua_lower: final_referer = "Remember App"
        elif "kakaotalk" in ua_lower: final_referer = "KakaoTalk"
        elif "instagram" in ua_lower: final_referer = "Instagram"
        elif "naver" in ua_lower: final_referer = "Naver App"
        elif not final_referer or final_referer == "Direct": final_referer = "Direct / Unknown"

        res = supabase.table("visitor_logs").select("visit_count").eq("ip_address", client_ip).eq("screen_id", payload.screen_id).execute()
        
        if res.data:
            current_count = res.data[0]['visit_count']
            supabase.table("visitor_logs").update({
                "visit_count": current_count + 1,
                "last_visit": kst_time,
                "referer": final_referer
            }).eq("ip_address", client_ip).eq("screen_id", payload.screen_id).execute()
        else:
            supabase.table("visitor_logs").insert({
                "ip_address": client_ip,
                "country": country,
                "city": city,
                "referer": final_referer,
                "screen_id": payload.screen_id,
                "last_visit": kst_time
            }).execute()
    except Exception as e:
        print(f"Visitor logging error: {e}")

@app.post("/api/log-visit")
async def log_visit(request: Request, body: VisitLogRequest, background_tasks: BackgroundTasks):
    forwarded_for = request.headers.get("x-forwarded-for")
    client_ip = forwarded_for.split(",")[0].strip() if forwarded_for else request.client.host
    background_tasks.add_task(_process_log_visit, client_ip, body)
    return {"status": "success"}

# ==============================================================================
# 🌟 스마트 캐시
# ==============================================================================
class SmartCache:
    def __init__(self):
        self.lock = threading.Lock()
        self.data = None
        self.last_ts = None

news_smart_cache = SmartCache()
quant_smart_cache = SmartCache()

krx_cache = TTLCache(maxsize=5, ttl=86400)
funda_cache = TTLCache(maxsize=3000, ttl=50)

def _get_latest_news_ts():
    res = supabase.table("market_news").select("created_at").order("created_at", desc=True).limit(1).execute()
    if res.data: return res.data[0]["created_at"]
    return None

def _fetch_all_news(limit: int = 500):
    res = supabase.table("market_news").select("*").order("created_at", desc=True).limit(limit).execute()
    return res.data

def refresh_news_cache(limit: int = 500):
    if not supabase: return
    try:
        latest_ts = _get_latest_news_ts()
        data = _fetch_all_news(limit)
        with news_smart_cache.lock:
            news_smart_cache.data = data
            news_smart_cache.last_ts = latest_ts
        print(f"✅ [뉴스 캐시 예열] {latest_ts}")
    except Exception as e:
        print(f"⚠️ 뉴스 캐시 예열 실패: {e}")

def _get_latest_quant_ts():
    res = supabase.table("quant_screening_cache").select("updated_at").in_("id", [1, 2, 11, 12, 13]).order("updated_at", desc=True).limit(1).execute()
    if res.data: return res.data[0]["updated_at"]
    return None

def _fetch_all_quant():
    data = {"holdings": [], "trades": [], "history": [], "confirmed": [], "watchlist": []}
    r1 = supabase.table("quant_screening_cache").select("id, results").in_("id", [11, 12, 13]).execute()
    if r1.data:
        for row in r1.data:
            try:
                res_json = json.loads(row["results"])
                if row["id"] == 11: data["holdings"] = res_json
                elif row["id"] == 12: data["trades"] = res_json
                elif row["id"] == 13: data["history"] = res_json
            except: pass
    r2 = supabase.table("quant_screening_cache").select("id, results").in_("id", [1, 2]).execute()
    if r2.data:
        for row in r2.data:
            try:
                res_json = json.loads(row["results"])
                if row["id"] == 1: data["confirmed"] = res_json
                elif row["id"] == 2: data["watchlist"] = res_json
            except: pass
    return data

def refresh_quant_cache():
    if not supabase: return
    try:
        latest_ts = _get_latest_quant_ts()
        data = _fetch_all_quant()
        with quant_smart_cache.lock:
            quant_smart_cache.data = data
            quant_smart_cache.last_ts = latest_ts
        print(f"✅ [퀀트 캐시 예열] {latest_ts}")
    except Exception as e:
        print(f"⚠️ 퀀트 캐시 예열 실패: {e}")

def decrypt_text(encrypted_text: str) -> str:
    if not encrypted_text or not cipher_suite:
        return encrypted_text
    try:
        return cipher_suite.decrypt(encrypted_text.encode('utf-8')).decode('utf-8')
    except Exception:
        return encrypted_text

@app.get("/api/news")
def get_news(limit: int = 500, refresh: str = "false"):
    if not supabase: return {"status": "error", "message": "DB 설정 안됨"}
    try:
        if refresh.lower() == "true":
            refresh_news_cache(limit)
            with news_smart_cache.lock:
                return {"status": "success", "data": news_smart_cache.data, "cached": False}
        latest_ts = _get_latest_news_ts()
        with news_smart_cache.lock:
            if news_smart_cache.data is not None and news_smart_cache.last_ts == latest_ts:
                return {"status": "success", "data": news_smart_cache.data, "cached": True}
        data = _fetch_all_news(limit)
        with news_smart_cache.lock:
            news_smart_cache.data = data
            news_smart_cache.last_ts = latest_ts
        return {"status": "success", "data": data, "cached": False}
    except Exception as e: return {"status": "error", "message": str(e)}


@app.get("/api/quant-dashboard")
def get_quant_dashboard(refresh: str = "false"):
    if not supabase: return {"status": "error", "message": "DB 설정 안됨"}
    try:
        if refresh.lower() == "true":
            refresh_quant_cache()
            with quant_smart_cache.lock:
                return {"status": "success", "data": quant_smart_cache.data}
        latest_ts = _get_latest_quant_ts()
        with quant_smart_cache.lock:
            if quant_smart_cache.data is not None and quant_smart_cache.last_ts == latest_ts:
                return {"status": "success", "data": quant_smart_cache.data, "cached": True}
        data = _fetch_all_quant()
        with quant_smart_cache.lock:
            quant_smart_cache.data = data
            quant_smart_cache.last_ts = latest_ts
        return {"status": "success", "data": data, "cached": False}
    except Exception as e: return {"status": "error", "message": str(e)}


@app.get("/api/krx-list")
@cached(cache=krx_cache)
def get_krx_list(refresh: str = "false"):
    if refresh.lower() == "true": krx_cache.clear()
    if not supabase: return {"status": "error", "message": "DB 설정 안됨"}
    try:
        res = supabase.table("quant_screening_cache").select("results").eq("id", 99).execute()
        if res.data: return {"status": "success", "data": json.loads(res.data[0]["results"])}
        return {"status": "success", "data": []}
    except Exception as e: return {"status": "error", "message": str(e)}


# ==============================================================================
# ⚡ 4. 실시간 개별 종목/지수 분석 리포트 API 
# ==============================================================================
@app.get("/api/search/{symbol}")
def search_stock(symbol: str, t: Optional[str] = None):
    if not supabase: return {"status": "error", "message": "DB 설정 안됨"}

    if symbol in funda_cache:
        return {"status": "success", "data": funda_cache[symbol], "cached": True}

    try:
        is_index = symbol in ['KS11', 'KQ11', 'US500', 'IXIC', 'DJI']
        
        df_price = pd.DataFrame()
        curr_price, prev_close, ret_1d, ret_1m = 0.0, 0.0, 0.0, 0.0
        chart_data = []
        market_status = "장마감"
        
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}

        if is_index:
            if symbol in ['KS11', 'KQ11']:
                naver_sym = "KOSPI" if symbol == 'KS11' else "KOSDAQ"
                try:
                    url = f"https://polling.finance.naver.com/api/realtime/domestic/index/{naver_sym}"
                    req = urllib.request.Request(url, headers=headers)
                    with urllib.request.urlopen(req, timeout=5) as response:
                        data = json.loads(response.read().decode('utf-8'))
                        ds = data['datas'][0]
                        curr_price = float(ds['closePrice'].replace(',', ''))
                        ret_1d = float(ds['fluctuationsRatio'])
                        m_stat = ds.get('marketStatus', 'CLOSE')
                        market_status = "장중" if m_stat == "OPEN" else "장마감"
                except Exception as e:
                    print("Naver API error:", e)

                now_k = now_kst()
                if now_k.weekday() >= 5 or now_k.hour < 9 or (now_k.hour == 15 and now_k.minute > 30) or now_k.hour > 15:
                    market_status = "장마감"

                try:
                    df_price = fdr.DataReader(symbol, (now_kst() - timedelta(days=150)).strftime('%Y-%m-%d'))
                    if not df_price.empty:
                        if len(df_price) >= 21:
                            ret_1m = ((curr_price - float(df_price['Close'].iloc[-21])) / float(df_price['Close'].iloc[-21]) * 100)
                        for idx, row in df_price.tail(120).iterrows():
                            chart_data.append({"date": idx.strftime("%Y-%m-%d"), "price": float(row['Close'])})
                except: pass

            else:
                yahoo_ticker_map = {'US500': '^GSPC', 'IXIC': '^IXIC', 'DJI': '^DJI'}
                y_sym = yahoo_ticker_map.get(symbol, symbol)
                
                try:
                    chart_url = f"https://query1.finance.yahoo.com/v8/finance/chart/{y_sym}?interval=1d&range=150d"
                    req = urllib.request.Request(chart_url, headers=headers)
                    with urllib.request.urlopen(req, timeout=5) as response:
                        c_data = json.loads(response.read().decode('utf-8'))['chart']['result'][0]
                        
                        meta = c_data['meta']
                        curr_price = float(meta['regularMarketPrice'])
                        
                        try:
                            trading_periods = meta.get('currentTradingPeriod', {}).get('regular', {})
                            start_ts = trading_periods.get('start', 0)
                            end_ts = trading_periods.get('end', 0)
                            now_ts = int(datetime.utcnow().timestamp())
                            
                            if start_ts <= now_ts < end_ts:
                                market_status = "장중"
                            else:
                                market_status = "장마감"
                        except Exception as e:
                            print("US Market Status Parse Error:", e)
                            market_status = "장마감"
                        
                        timestamps = c_data.get('timestamp', [])
                        closes = c_data['indicators']['quote'][0].get('close', [])
                        
                        if timestamps and closes:
                            df_price = pd.DataFrame({'Close': closes}, index=pd.to_datetime(timestamps, unit='s', utc=True).tz_convert('Asia/Seoul'))
                            df_price = df_price.dropna()
                            
                            if len(df_price) >= 2:
                                prev_close = float(df_price['Close'].iloc[-2])
                                ret_1d = ((curr_price - prev_close) / prev_close) * 100
                            
                            if len(df_price) >= 21:
                                ret_1m = ((curr_price - float(df_price['Close'].iloc[-21])) / float(df_price['Close'].iloc[-21]) * 100)
                            
                            for idx, row in df_price.tail(120).iterrows():
                                chart_data.append({"date": idx.strftime("%Y-%m-%d"), "price": float(row['Close'])})
                except Exception as e:
                    print("Yahoo Chart error:", e)
                    try:
                        df_price = fdr.DataReader(symbol, (now_kst() - timedelta(days=150)).strftime('%Y-%m-%d'))
                        if not df_price.empty:
                            curr_price = float(df_price['Close'].iloc[-1])
                            prev_close = float(df_price['Close'].iloc[-2])
                            ret_1d = ((curr_price - prev_close) / prev_close) * 100
                            if len(df_price) >= 21:
                                ret_1m = ((curr_price - float(df_price['Close'].iloc[-21])) / float(df_price['Close'].iloc[-21]) * 100)
                            for idx, row in df_price.tail(120).iterrows():
                                chart_data.append({"date": idx.strftime("%Y-%m-%d"), "price": float(row['Close'])})
                    except: pass

            index_names = {'KS11': '코스피', 'KQ11': '코스닥', 'US500': 'S&P 500', 'IXIC': 'NASDAQ'}
            result_data = {
                "symbol": symbol,
                "name": index_names.get(symbol, symbol),
                "current_price": curr_price,
                "ret_1d": ret_1d,
                "ret_1m": ret_1m,
                "score": 0.0,
                "gates": {},
                "fundamental": {},
                "chart_data": chart_data,
                "market": "Index",
                "sector": "지수",
                "market_status": market_status
            }
            funda_cache[symbol] = result_data
            return {"status": "success", "data": result_data}

        else:
            df_price = load_price_from_db(supabase, symbol)
            if df_price.empty:
                try: df_price = fdr.DataReader(symbol, (now_kst() - timedelta(days=300)).strftime('%Y-%m-%d'))
                except: pass

            if df_price.empty:
                return {"status": "error", "message": "차트 데이터를 불러올 수 없습니다."}

            for idx, row in df_price.tail(120).iterrows():
                chart_data.append({"date": idx.strftime("%Y-%m-%d"), "price": float(row['Close'])})

            curr_price = float(df_price['Close'].iloc[-1])
            if len(df_price) >= 2: ret_1d = ((curr_price - float(df_price['Close'].iloc[-2])) / float(df_price['Close'].iloc[-2]) * 100)
            if len(df_price) >= 21: ret_1m = ((curr_price - float(df_price['Close'].iloc[-21])) / float(df_price['Close'].iloc[-21]) * 100)

            naver_fund = fetch_naver_fundamental(symbol)
            sector = naver_fund.get("sector", "") if naver_fund else ""

            metrics = calc_quant_metrics(df_price, naver_fund)
            score = 0.0
            gates = {}

            if metrics and metrics.get("ma20", 0) > 0:
                f_growth = bool(metrics["growth_composite"] > 0)
                f_mdd    = bool(metrics["mdd"] >= metrics["dynamic_mdd_limit"])
                f_liq    = bool(metrics["liquidity_20d"] >= 50)
                f_trend  = bool((curr_price > metrics["ma20"]) and (metrics["ma20"] > metrics["ma60"]))
                f_break  = bool(curr_price >= (metrics["high_60d"] * 0.90))
                f_vol    = bool(metrics["vol_5d"] > (metrics["vol_60d"] * 1.5))

                gates = {
                    'A': {'name': 'Growth Composite', 'pass': f_growth, 'reason': f"Comp {metrics.get('growth_composite',0):+.1f}%"},
                    'B': {'name': 'Dynamic MDD', 'pass': f_mdd, 'reason': f"MDD {metrics.get('mdd',0):.1f}% (Limit: {metrics.get('dynamic_mdd_limit',0):.1f}%)"},
                    'C': {'name': 'Liquidity', 'pass': f_liq, 'reason': f"{metrics.get('liquidity_20d',0):,.0f}억"},
                    'D': {'name': 'Trend Alignment', 'pass': f_trend, 'reason': "Price > 20MA > 60MA" if f_trend else "추세 미달"},
                    'E': {'name': 'Price Breakout', 'pass': f_break, 'reason': f"고점대비 {(curr_price/metrics.get('high_60d',1))*100:.1f}%" if metrics.get('high_60d') else "-"},
                    'F': {'name': 'Volume Surge', 'pass': f_vol, 'reason': f"Vol {metrics.get('vol_5d',0)/metrics.get('vol_60d',1):.1f}x 급증" if metrics.get('vol_60d') else "-"}
                }
                pass_count = sum([1 for g in gates.values() if g['pass']])
                mom = ((curr_price - metrics["ma60"]) / metrics["ma60"] * 100) if metrics["ma60"] > 0 else 0
                score = float(min(99.9, max(0, (pass_count/6 * 50) + min(25, max(0, metrics.get("net_yoy", 0)/5)) + min(25, max(0, mom)))))

            r_krx = supabase.table("quant_screening_cache").select("results").eq("id", 99).execute()
            stock_name = symbol
            market = "KOSPI"
            if r_krx.data:
                krx_list = json.loads(r_krx.data[0]["results"])
                matched = next((item for item in krx_list if item["Symbol"] == symbol), None)
                if matched:
                    stock_name = matched.get("Name", symbol)
                    market = matched.get("Market", "KOSPI")

            result_data = {
                "symbol": symbol, "name": stock_name, "current_price": curr_price, "ret_1d": ret_1d, "ret_1m": ret_1m,
                "score": score, "gates": gates, "fundamental": naver_fund, "chart_data": chart_data,
                "market": market, "sector": sector, "market_status": "장마감"
            }
            funda_cache[symbol] = result_data
            return {"status": "success", "data": result_data}

    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"status": "error", "message": str(e)}

# ==============================================================================
# 🏢 5. 부동산 아파트 실거래가 스캔 API (GET 스트리밍 원복 및 Render 타임아웃 방어 처리)
# ==============================================================================
@app.get("/api/realestate/build-stream")
async def build_realestate_stream(gu_code: str, gu_name: str, dong: str, start_date: str, end_date: str, filters: str = ""):
    async def event_generator():
        try:
            res = supabase.table("user_api_keys").select("rtms_key").eq("username", "admin").execute()
            if not res.data or not res.data[0].get("rtms_key"):
                yield f"data: {json.dumps({'status': 'error', 'message': 'API 키가 설정되지 않았습니다.'})}\n\n"
                return

            encrypted_key = res.data[0]["rtms_key"]
            rtms_key = decrypt_text(encrypted_key)

            s_date = datetime.strptime(start_date, "%Y-%m-%d")
            e_date = datetime.strptime(end_date, "%Y-%m-%d")
            apt_filters = [f.strip() for f in filters.split(',')] if filters.strip() else []

            gen = generate_excel_data(rtms_key, gu_code, gu_name, dong, s_date, e_date, apt_filters)
            
            # 🌟 Render의 100초 타임아웃 방어를 위한 Ping(더미 메시지)용 시간 추적
            last_yield_time = datetime.now()

            for status, payload in gen:
                # 🌟 현재 시간과 마지막으로 보낸 시간을 비교하여, 15초 이상 지연되면 무조건 ping 로그를 보냄
                current_time = datetime.now()
                if (current_time - last_yield_time).total_seconds() > 15:
                    yield f"data: {json.dumps({'status': 'progress', 'message': '...연결 유지용 Ping 전송 및 K-APT 데이터 크롤링 대기 중...'})}\n\n"
                    await asyncio.sleep(0.01)
                    last_yield_time = datetime.now()

                if status == "progress" or status == "log":
                    yield f"data: {json.dumps({'status': 'log', 'message': payload})}\n\n"
                    last_yield_time = datetime.now()
                    await asyncio.sleep(0.1) # 버퍼링 방지용 약간의 지연 (필수)
                elif status == "error":
                    yield f"data: {json.dumps({'status': 'error', 'message': payload})}\n\n"
                    return
                elif status == "success":
                    app.state.last_excel_data = payload["data"]
                    app.state.last_excel_filename = payload["filename"]
                    yield f"data: {json.dumps({'status': 'done', 'message': '엑셀 생성 완료!'})}\n\n"
                    return

        except Exception as e:
            yield f"data: {json.dumps({'status': 'error', 'message': str(e)})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")

@app.get("/api/realestate/download")
def download_realestate():
    excel_data = getattr(app.state, "last_excel_data", None)
    filename = getattr(app.state, "last_excel_filename", "부동산_실거래가.xlsx")
    
    if not excel_data:
        return {"status": "error", "message": "다운로드할 데이터가 없습니다."}
        
    encoded_filename = urllib.parse.quote(filename)
    return Response(
        content=excel_data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}"}
    )

scheduler = BackgroundScheduler(timezone=KST)
BATCH_WARMUP_TIMES = [(8, 10), (14, 40), (16, 40), (23, 40)]

def _warmup_all():
    refresh_news_cache()
    refresh_quant_cache()

for hour, minute in BATCH_WARMUP_TIMES:
    scheduler.add_job(_warmup_all, "cron", hour=hour, minute=minute)

@app.on_event("startup")
def start_scheduler():
    if supabase: _warmup_all()
    scheduler.start()

@app.on_event("shutdown")
def stop_scheduler():
    scheduler.shutdown(wait=False)

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
