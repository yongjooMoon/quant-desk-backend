import os
import json
import hashlib
import random
import threading
import time
import math
import gc
import ctypes
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
from typing import Optional
from cachetools import TTLCache, cached
from apscheduler.schedulers.background import BackgroundScheduler
import pandas as pd
import re

from cryptography.fernet import Fernet

# 기존 quant_core 및 real_estate 모듈 활용 (decrypt_text 제거, 기존 방식 복구)
from quant_core import (
    load_price_from_db, fetch_naver_fundamental, now_kst,
    load_fundamental_from_db, save_fundamental_to_db,
    evaluate_entry_gates, get_index_return_pct, load_filtered_universe, load_market_regime_cache,
    fetch_all_rows,   # ← 추가 (quant_core로 옮긴 공통 유틸 재사용)
    load_full_krx_universe,  # /api/krx-list가 호출하지만 import가 빠져 있어 NameError로 죽어있던 것 수정
)
from real_estate import generate_excel_data

load_dotenv()

app = FastAPI()

# [2026-10-23 perf] HTTP cache for the public read-only market data: weak ETag over the body + `Cache-Control: private, no-cache`.
# The browser revalidates with If-None-Match and gets a body-less 304 when nothing changed (screener 327 KB gzip, chart 100 KB, ...).
# no-cache (not max-age) on purpose: the refresh button and the 14:30 batch update are visible immediately. Registered BEFORE CORSMiddleware
# so that CORS is the outer layer and its headers also go on the 304. Authenticated / mutating routes are not in the allow-list.
_ETAG_PREFIXES = ("/api/macro", "/api/screener", "/api/backtesting", "/api/market/", "/api/krx-list", "/api/quant-dashboard", "/api/search/")


@app.middleware("http")
async def etag_middleware(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if request.method != "GET" or response.status_code != 200 or not path.startswith(_ETAG_PREFIXES):
        return response
    if "json" not in (response.headers.get("content-type") or ""):
        return response
    body = b"".join([chunk async for chunk in response.body_iterator])
    etag = 'W/"' + hashlib.md5(body).hexdigest() + '"'
    cache_headers = {"ETag": etag, "Cache-Control": "private, no-cache"}
    inm = request.headers.get("if-none-match")
    if inm and etag in [x.strip() for x in inm.split(",")]:
        return Response(status_code=304, headers=cache_headers)
    headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
    headers.update(cache_headers)
    return Response(content=body, status_code=200, headers=headers, media_type=response.media_type)


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
_quant_ts_checked = {"t": 0.0}          # time of the last freshness check of quant_smart_cache (see get_quant_dashboard)
QUANT_TS_RECHECK_SEC = 60

krx_cache = TTLCache(maxsize=5, ttl=86400)
krx_cache_lock = threading.Lock()  # [2026-09-17] cachetools @cached는 기본적으로 스레드 안전하지 않음
# [2026-09-17] OOM 대응: maxsize=3000은 실사용 대비 과도하게 큰 상한이었다(50초 안에
# 3000개 서로 다른 종목이 조회될 일은 사실상 없음) — 캐시 자체가 무제한처럼 동작해
# 메모리 상한 역할을 못 하고 있었다. 실사용 패턴에 맞춰 상한/TTL을 축소.
funda_cache = TTLCache(maxsize=200, ttl=30)

# 🧪 [추가] 시장 지수(KOSPI/KOSDAQ) 60일 수익률(RS 벤치마크) 캐시 — 종목 검색마다 fdr을
#    다시 호출하지 않도록 짧은 TTL로 캐싱 (배치와 동일한 get_index_return_pct 사용)
index_return_cache = TTLCache(maxsize=2, ttl=600)

# 뉴스 무한스크롤 캐시 상한 — 이 이상 과거로 스크롤하면 더 못 불러온다(총량이 몇 주~몇 달치인
# 뉴스 피드에서 3000개는 사실상 도달하기 어려운 한도라 실용적 상한으로 채택).
NEWS_CACHE_CAP = 3000

def _get_latest_news_ts():
    res = supabase.table("market_news").select("created_at").order("created_at", desc=True).limit(1).execute()
    if res.data: return res.data[0]["created_at"]
    return None

def _fetch_all_news(limit: int = 500):
    res = supabase.table("market_news").select("*").order("created_at", desc=True).limit(limit).execute()
    return res.data

def refresh_news_cache(limit: int = NEWS_CACHE_CAP):
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
                res_json = _sanitize_nan(json.loads(row["results"]))
                if row["id"] == 11: data["holdings"] = res_json
                elif row["id"] == 12: data["trades"] = res_json
                elif row["id"] == 13: data["history"] = res_json
            except: pass
    r2 = supabase.table("quant_screening_cache").select("id, results").in_("id", [1, 2, 22]).execute()
    if r2.data:
        for row in r2.data:
            try:
                res_json = json.loads(row["results"])
                if row["id"] == 1: data["confirmed"] = res_json
                elif row["id"] == 2: data["watchlist"] = res_json
                elif row["id"] == 22:   # nA3 regime ON/OFF per market (only these fields leave the snapshot; gate bits stay private)
                    mo = res_json.get("market_open") or {}
                    data["regime"] = {"KOSPI": bool(mo.get("KOSPI")), "KOSDAQ": bool(mo.get("KOSDAQ")), "asof": res_json.get("asof"), "d2_hot_day": bool(res_json.get("d2_hot_day"))}
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
            _quant_ts_checked["t"] = time.time()
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
def get_news(offset: int = 0, limit: int = 50, major_only: bool = False, today_only: bool = False, refresh: str = "false"):
    """
    offset/limit: 무한스크롤 페이지네이션 (NewsDesk.jsx 세로 리스트).
    major_only=true: 오늘(KST) is_major 뉴스만 — 히어로 레일 전용, offset/limit 무시.
    today_only=true: is_major 여부와 무관하게 오늘(KST) 수집된 전체 건수만 — NewsDesk
    히어로 밴드의 "금일 수집" 통계 전용. 기사 본문은 필요 없으므로 data는 빈 배열로
    돌려주고 total만 본다(캐시가 이미 최신순 상위 N개를 들고 있어 별도 쿼리 없음).
    캐시 구조: news_smart_cache.data는 "최신순 상위 N개"를 통째로 들고 있는 인메모리
    캐시(최대 NEWS_CACHE_CAP개)이고, 매 요청은 여기서 파이썬 슬라이싱만 한다 — 페이지마다
    Supabase를 새로 때리지 않고, 캐시가 이번 페이지를 못 채울 때만 더 크게 다시 채운다.
    """
    if not supabase: return {"status": "error", "message": "DB 설정 안됨"}
    try:
        if refresh.lower() == "true":
            refresh_news_cache(NEWS_CACHE_CAP)
        else:
            latest_ts = _get_latest_news_ts()
            with news_smart_cache.lock:
                cache_fresh = news_smart_cache.data is not None and news_smart_cache.last_ts == latest_ts
                cache_covers_page = cache_fresh and len(news_smart_cache.data) >= offset + limit
            if not cache_covers_page:
                needed = min(max(offset + limit, 500), NEWS_CACHE_CAP)
                refresh_news_cache(needed)

        with news_smart_cache.lock:
            full = news_smart_cache.data or []

        if major_only:
            # market_news.created_at은 실제로는 "+00:00"이 붙어서 내려온다(주식/sync_news_to_supabase.py가
            # naive datetime.isoformat()으로 기록한 걸 Postgres timestamptz가 UTC로 라벨링해서 저장).
            # 하지만 그 naive 값 자체가 이미 KST 벽시계 시각으로 채워진 것이고, 프론트의 parseDBTime도
            # +00:00을 무시한 채 앞자리 숫자를 그대로 로컬(KST) 시각으로 읽는 관례가 이미 있다 — 그래서
            # 여기서도 astimezone 변환 없이 앞 10자리(YYYY-MM-DD)만 그 관례대로 비교해 "오늘(KST)"을 판정한다.
            today_kst_str = datetime.now(KST).date().isoformat()
            page = [n for n in full if n.get("is_major") and (n.get("created_at") or "")[:10] == today_kst_str]
            return {"status": "success", "data": page, "total": len(page), "has_more": False}

        if today_only:
            today_kst_str = datetime.now(KST).date().isoformat()
            count = sum(1 for n in full if (n.get("created_at") or "")[:10] == today_kst_str)
            return {"status": "success", "data": [], "total": count, "has_more": False}

        page = full[offset: offset + limit]
        return {
            "status": "success",
            "data": page,
            "total": len(full),
            "has_more": (offset + limit) < len(full),
            "offset": offset,
            "limit": limit,
        }
    except Exception as e: return {"status": "error", "message": str(e)}


@app.get("/api/quant-dashboard")
def get_quant_dashboard(refresh: str = "false"):
    if not supabase: return {"status": "error", "message": "DB 설정 안됨"}
    try:
        if refresh.lower() == "true":
            refresh_quant_cache()
            with quant_smart_cache.lock:
                # [2026-09-17] 다른 두 반환 경로와 일관되게 "cached" 필드 추가 — 강제 새로고침
                # 직후의 값이므로 방금 새로 채운 것과 같은 의미로 False.
                return {"status": "success", "data": quant_smart_cache.data, "cached": False}
        # [2026-10-23 perf] the freshness check below is one DB round trip per request (1.0-1.6 s measured). While the background job
        # (_quant_recheck, every QUANT_TS_RECHECK_SEC) is alive and has checked recently, serve the cache without touching the DB.
        if quant_smart_cache.data is not None and time.time() - _quant_ts_checked["t"] < QUANT_TS_RECHECK_SEC * 3:
            return {"status": "success", "data": quant_smart_cache.data, "cached": True}
        latest_ts = _get_latest_quant_ts()
        with quant_smart_cache.lock:
            if quant_smart_cache.data is not None and quant_smart_cache.last_ts == latest_ts:
                _quant_ts_checked["t"] = time.time()
                return {"status": "success", "data": quant_smart_cache.data, "cached": True}
            # [2026-09-17] 조회(_fetch_all_quant)도 같은 락 안에서 수행 — 캐시 만료 직후
            # 동시에 들어온 요청들이 전부 stale 판정을 내리고 각자 Supabase를 중복 조회하던
            # 것(thundering herd)을 방지. 이 엔드포인트는 호출 빈도가 낮아 락을 조회 시간만큼
            # 길게 잡아도 문제되지 않음.
            data = _fetch_all_quant()
            quant_smart_cache.data = data
            quant_smart_cache.last_ts = latest_ts
            _quant_ts_checked["t"] = time.time()
        return {"status": "success", "data": data, "cached": False}
    except Exception as e: return {"status": "error", "message": str(e)}

# [2026-09-17] 캐싱 대상(_get_krx_list_cached)과 에러 응답 변환(get_krx_list)을 분리.
# 기존엔 @cached가 걸린 함수 안에서 예외를 잡아 {"status":"error",...}를 정상 반환값처럼
# 리턴했는데, cachetools는 반환값의 성공/실패를 구분하지 않고 그대로 캐시해버려서 일시적
# 오류(FDR 캐시 지연 등) 한 번이면 이 에러 응답이 TTL(24시간) 내내 굳어버리는 문제가 있었다.
# 예외를 raise한 채로 두면 cachetools가 캐시하지 않으므로(공식 동작), 캐싱은 성공한
# 결과에만 걸리고 실패는 매 요청마다 새로 재시도된다.
@cached(cache=krx_cache, lock=krx_cache_lock)
def _get_krx_list_cached():
    df = load_full_krx_universe()
    records = [
        {
            "Symbol": row["Symbol"],
            "Name": row["Name"],
            "Market": row.get("Market", "-"),
            "SearchStr": f'{row["Name"]} ({row["Symbol"]})',
        }
        for _, row in df.iterrows()
    ]
    return {"status": "success", "data": records}

@app.get("/api/krx-list")
def get_krx_list(refresh: str = "false"):
    if refresh.lower() == "true":
        with krx_cache_lock:
            krx_cache.clear()
    try:
        return _get_krx_list_cached()
    except Exception as e:
        return {"status": "error", "message": str(e)}

# ==============================================================================
# ⚡ 4. 실시간 개별 종목/지수 분석 리포트 API 
# ==============================================================================

# 🧪 [추가] 오늘 배치가 이미 스크리닝해 둔 confirmed/watchlist 캐시에서 종목을 찾는다.
#    있으면 그 결과(정확한 factor_score + filter_details)를 그대로 재사용해서 중복계산을 피한다.
def _find_in_screening_cache(symbol: str) -> Optional[dict]:
    with quant_smart_cache.lock:
        cache_data = quant_smart_cache.data
    if not cache_data:
        return None
    for c in (cache_data.get("confirmed") or []):
        if c.get("symbol") == symbol:
            return c
    for c in (cache_data.get("watchlist") or []):
        if c.get("symbol") == symbol:
            return c
    return None

# 🧪 [추가] quant_core.run_screening_from_db()가 candidate["filter_details"]에 저장하는
#    {"Growth Composite": {...}, "Dynamic MDD": {...}, ...} 형태를 프론트가 기대하는
#    {'A': {...}, 'B': {...}, ...} 형태로 변환 (evaluate_entry_gates()의 반환 형식과 동일하게 맞춤)
def _map_filter_details_to_gates(filter_details: dict) -> dict:
    labels = [chr(65 + i) for i in range(26)]   # A, B, C ... (the active nA3 strategy has 13 conditions; the legacy one had 6)
    mapped = {}
    for i, (name, detail) in enumerate(filter_details.items()):
        if i >= len(labels):
            break
        mapped[labels[i]] = {"name": name, "pass": detail.get("pass", False), "reason": detail.get("reason", "")}
    return mapped

# 🧪 [추가] 배치(quant_cron.py)와 동일하게 시장별(KOSPI/KOSDAQ) RS 벤치마크를 분리해서 조회.
#    fdr 호출 비용을 줄이기 위해 10분 TTL 캐시를 둔다.
def _get_benchmark_return(is_kosdaq: bool) -> float:
    key = "KQ11" if is_kosdaq else "KS11"
    if key in index_return_cache:
        return index_return_cache[key]
    val = get_index_return_pct(key)
    index_return_cache[key] = val
    return val


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

            # 종목명/시장(코스피·코스닥) — 벤치마크 선택과 DB 저장에 필요해서 먼저 조회
            r_krx = supabase.table("quant_screening_cache").select("results").eq("id", 99).execute()
            stock_name = symbol
            market = "KOSPI"
            if r_krx.data:
                krx_list = json.loads(r_krx.data[0]["results"])
                matched = next((item for item in krx_list if item["Symbol"] == symbol), None)
                if matched:
                    stock_name = matched.get("Name", symbol)
                    market = matched.get("Market", "KOSPI")

            # ── 🧪 [1] 오늘 배치 스크리닝(confirmed/watchlist)에 이미 있는 종목이면
            #          그 결과(정확한 factor_score + filter_details)를 그대로 사용
            cached_screen = _find_in_screening_cache(symbol)
            if stock_name == symbol and cached_screen and cached_screen.get("name"):   # names missing from the id-99 master list (small caps) come from the batch candidate row
                stock_name = cached_screen["name"]

            # ── 🧪 [2] DB에 캐시된 펀더멘탈(배치가 DART+네이버 조합해 저장, 90일 TTL) 우선 사용
            fund = load_fundamental_from_db(supabase, symbol)
            if fund is None:
                # ── 🧪 [3] 캐시도 없을 때만 네이버 실시간 스크래핑 + 다음부터 쓰도록 DB에 저장
                fund = fetch_naver_fundamental(symbol) or {}
                try:
                    if fund:
                        save_fundamental_to_db(supabase, symbol, stock_name, fund)
                except Exception as e:
                    print(f"펀더멘탈 캐시 저장 실패({symbol}): {e}")

            sector = fund.get("sector", "") if fund else ""
            if not sector and cached_screen:
                sector = cached_screen.get("sector", "") or ""

            if cached_screen is not None:
                # 배치가 이미 계산해 둔 정확한 랭킹 스코어/게이트를 그대로 사용 (중복계산 방지 + 완전 동일 로직 보장)
                score = float(cached_screen.get("factor_score", 0.0))
                gates = _map_filter_details_to_gates(cached_screen.get("filter_details", {}))
            else:
                # ── 🧪 [4] 배치 대상이 아니었던 종목(예: 이미 보유 중이라 오늘 스크리닝 유니버스에서
                #          빠진 종목)은 core의 실제 추격매수 전략(evaluate_entry_gates)을 그대로 호출
                is_kosdaq = str(market).upper().startswith("KOSDAQ")
                benchmark_ret = _get_benchmark_return(is_kosdaq)
                gate_result = evaluate_entry_gates(df_price, fund, benchmark_ret_60d=benchmark_ret, symbol=symbol)

                if gate_result:
                    gates = gate_result["gates"]
                    pass_count = gate_result["pass_count"]
                    metrics = gate_result["metrics"]
                    mom = ((curr_price - metrics["ma60"]) / metrics["ma60"] * 100) if metrics["ma60"] > 0 else 0
                    score = float(min(99.9, max(0, (pass_count/max(1, len(gates)) * 50) + min(25, max(0, metrics.get("net_yoy", 0)/5)) + min(25, max(0, mom)))))
                else:
                    gates, score = {}, 0.0

            result_data = {
                "symbol": symbol, "name": stock_name, "current_price": curr_price, "ret_1d": ret_1d, "ret_1m": ret_1m,
                "score": score, "gates": gates, "fundamental": fund, "chart_data": chart_data,
                "market": market, "sector": sector, "market_status": "장마감"
            }
            funda_cache[symbol] = result_data
            return {"status": "success", "data": result_data}

    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"status": "error", "message": str(e)}
        
INDICATORS = [
    "QQQ_PRICE", "QQQ_MA50", "QQQ_MA200", "QQQ_MA200_SLOPE",
    "REAL_YIELD_10Y", "CREDIT_SPREAD", "VIX", "WTI", "FEAR_GREED"
]

macro_cache = TTLCache(maxsize=1, ttl=300)      # the macro table changes a few times a day; the uncached build took 2.4-3.0 s per request (measured 2026-10-23)
macro_cache_lock = threading.Lock()
MACRO_LIST_HISTORY = 30                         # points per indicator in the list response (cards draw the last 20 and the change); the chart modal asks /api/macro/history/{indicator}


def _build_macro_full():
    """All indicators with up to 1300 history points each (the former /api/macro body). Raises on DB errors so a failure is never cached."""
    # 1. one query for every indicator: indicators x 1300 rows as the limit
    total_limit = len(INDICATORS) * 1300
    r = (supabase.table("macro_market_data")
         .select("*")
         .in_("indicator", INDICATORS)
         .order("recorded_at", desc=True)
         .limit(total_limit)
         .execute())
    if not r.data:
        return []

    macro_map = {}
    count_map = {}

    # 2. group the single result by indicator
    for row in r.data:
        ind = row["indicator"]

        # at most 1300 points per indicator
        if count_map.get(ind, 0) >= 1300:
            continue

        date_str = str(row["recorded_at"])[:10]
        val = float(row["value"] if row["value"] is not None else 0)

        # first sighting of an indicator (desc order, so the newest row)
        if ind not in macro_map:
            macro_map[ind] = {
                "indicator": ind,
                "display_name": row.get("display_name", ind),
                "source": row.get("source", ""),
                "value": val,
                "unit": row.get("unit", ""),
                "signal": row.get("signal") if row.get("signal") else "neutral",
                "recorded_at": date_str,
                "change_percent": 0.0,
                "history": []
            }
            count_map[ind] = 0

        # older points go to the front (ascending order)
        macro_map[ind]["history"].insert(0, {"date": date_str, "value": val})
        count_map[ind] += 1

    # 3. change vs the previous point
    for ind, data in macro_map.items():
        hist = data["history"]
        if len(hist) >= 2:
            last_val = hist[-1]["value"]  # newest (end of the array)
            prev_val = hist[-2]["value"]
            if prev_val != 0:
                data["change_percent"] = ((last_val - prev_val) / prev_val) * 100

    return list(macro_map.values())


def _macro_full_cached():
    with macro_cache_lock:
        if "v" in macro_cache: return macro_cache["v"]
    data = _build_macro_full()
    if data:                                    # an empty table is not cached
        with macro_cache_lock:
            macro_cache["v"] = data
    return data


@app.get("/api/macro")
def get_macro_dashboard(full: str = "false"):
    """Cards + ticker data. Default: history trimmed to the last MACRO_LIST_HISTORY points; full=true returns the old shape (all history)."""
    if not supabase:
        return {"status": "error", "message": "DB 설정 안됨"}
    try:
        data = _macro_full_cached()
        if full.lower() != "true":
            data = [{**d, "history": d["history"][-MACRO_LIST_HISTORY:]} for d in data]
        return {"status": "success", "data": data}
    except Exception as e:
        print(f"Macro fetch error: {e}")
        return {"status": "error", "message": str(e)}


@app.get("/api/macro/history/{indicator}")
def get_macro_history(indicator: str, days: int = 1260):
    """History of one indicator for the chart modal (last `days` points, ascending)."""
    if not supabase:
        return {"status": "error", "message": "DB 설정 안됨"}
    if indicator not in INDICATORS:
        return {"status": "error", "message": "unknown indicator"}
    try:
        for d in _macro_full_cached():
            if d["indicator"] == indicator:
                return {"status": "success", "indicator": indicator, "history": d["history"][-max(1, min(int(days), 1300)):]}
        return {"status": "success", "indicator": indicator, "history": []}
    except Exception as e:
        print(f"Macro history error: {e}")
        return {"status": "error", "message": str(e)}


backtest_result_cache = TTLCache(maxsize=1, ttl=300)  # 5분 캐시. 프로젝트 기존 TTL 값이 있으면 그걸로 맞추세요.
backtest_result_cache_lock = threading.Lock()

BACKTEST_12Y_CACHE_ID = 16  # quant_backtest_12y.py의 TBL_BACKTEST_12Y_CACHE_ID와 동일해야 함

# [2026-09-17] krx-list와 동일한 이유로 캐싱 대상/에러 변환 분리 — 실패를 raise로 전파해
# cachetools가 에러 응답을 캐시하지 않게 한다(성공한 결과만 5분간 캐시).
@cached(cache=backtest_result_cache, lock=backtest_result_cache_lock)
def _get_backtesting_12y_result_cached():
    if not supabase:
        raise RuntimeError("DB 설정 안됨")
    res = supabase.table("quant_screening_cache").select("results").eq("id", BACKTEST_12Y_CACHE_ID).execute()
    if res.data and res.data[0].get("results"):
        return {"status": "success", "data": json.loads(res.data[0]["results"])}
    return {"status": "success", "data": None}

@app.get("/api/backtesting/12y-result")
def get_backtesting_12y_result(refresh: str = "false"):
    if refresh.lower() == "true":
        with backtest_result_cache_lock:
            backtest_result_cache.clear()
    try:
        return _get_backtesting_12y_result_cached()
    except Exception as e:
        return {"status": "error", "message": str(e)}


# 🌟 배치가 하루 한 번(14:30)만 갱신하는 데이터이므로, 캐시 TTL을 그에 맞춰 넉넉히 잡아도 됨.
#    프론트에서 새로고침 버튼 누르면 refresh=true로 캐시를 비우고 다시 읽음.
SCREENER_CACHE_TTL = 3600
screener_result_cache = TTLCache(maxsize=1, ttl=SCREENER_CACHE_TTL)
screener_result_cache_lock = threading.Lock()

# [2026-09-17] 위 두 엔드포인트와 동일한 이유로 캐싱 대상/에러 변환 분리. 특히 이 엔드포인트는
# "배치가 아직 안 돌았거나 실패했을 수 있음" 케이스가 실제로 수 초~수 분 뒤 정상화되는
# 일시적 상태인데, 기존엔 이 메시지가 그대로 1시간(SCREENER_CACHE_TTL) 굳어서 실제로는
# 데이터가 채워진 뒤에도 화면에 계속 에러가 떴었다.
@cached(cache=screener_result_cache, lock=screener_result_cache_lock)
def _get_screener_data_cached():
    if not supabase:
        raise RuntimeError("DB 설정 안됨")
    results = fetch_all_rows(supabase.table("stock_screener_snapshot").select("*"))
    if not results:
        raise RuntimeError("stock_screener_snapshot이 비어있습니다 (배치가 아직 안 돌았거나 실패했을 수 있음)")

    # 레짐은 배치 시점 값이 스냅샷에 이미 박혀있지만, 장중 갱신 사이의 미세한 지연을
    # 없애기 위해 가벼운 단일 행 조회로 최신값을 덮어씀 (비용 거의 없음)
    regime_cache = load_market_regime_cache(supabase)
    is_2nd_buy_regime = bool(regime_cache.get("is_2nd_buy_regime", False))
    for row in results:
        row["is_second_buy_regime"] = is_2nd_buy_regime

    return {"status": "success", "data": results}

@app.get("/api/screener")
def get_screener_data(refresh: str = "false"):
    if refresh.lower() == "true":
        with screener_result_cache_lock:
            screener_result_cache.clear()
    try:
        return _get_screener_data_cached()
    except Exception as e:
        return {"status": "error", "message": str(e)}

# ==============================================================================
# 📊 [신규 2026-10-15] 증시 메뉴: 종목 목록(거래량/시총), MTS형 차트(일/주/월/년 + 이동평균), 애널리스트 목표가
#   - 목록: 로컬 배치(주식/market_list_publish.py, 평일 15:45 종가 수집 직후)가 quant_screening_cache id 23에 올린 마지막 2거래일 값 + stock_fundamental 시총
#   - 차트: stock_daily(약 720일) 일봉에서 주/월/년을 계산
#   - 목표가: 네이버 모바일 JSON API(비공식). 종목을 열 때만 조회, 24시간 캐시, 실패하면 10분간 재호출 없이 "없음" (재시도 루프 없음), 요청 간 0.4초
#   - [2026-10-22] KIS invest-opinion (needs KIS_APP_KEY / KIS_APP_SECRET env) is tried first: latest target per broker over 180 days; Naver is the fallback
# ==============================================================================
market_list_cache = TTLCache(maxsize=1, ttl=300)
market_list_lock = threading.Lock()
target_cache = TTLCache(maxsize=1000, ttl=86400)
target_fail_cache = TTLCache(maxsize=1000, ttl=600)
target_lock = threading.Lock()
_SYMBOL_RE = re.compile(r"^[0-9A-Z]{6}$")      # KRX alphanumeric codes (e.g. 0013Y0) as well as 6-digit codes


def _build_market_list():
    r = supabase.table("quant_screening_cache").select("results").eq("id", 23).execute()
    if not r.data:
        raise RuntimeError("증시 목록 캐시(id 23)가 없습니다 (평일 15:45 종가 수집 이후 생성)")
    base = json.loads(r.data[0]["results"])
    marcap = {}
    try:
        for x in fetch_all_rows(supabase.table("stock_fundamental").select("symbol,marcap_억")):
            v = x.get("marcap_억")
            if v is not None:
                marcap[x["symbol"]] = float(v)
    except Exception as e:
        print(f"[market-list] marcap join failed: {type(e).__name__}")
    rows = []
    for code, name, mk, close, pc, vol, amt in base["rows"]:
        rows.append({"symbol": code, "name": name, "market": mk, "close": close, "chg_pct": (round((close / pc - 1) * 100, 2) if pc else None),
                     "volume": vol, "amount": amt, "marcap_eok": marcap.get(code)})
    return {"asof": base["asof"], "rows": rows}


@app.get("/api/market/list")
def get_market_list(market: str = "ALL", sort: str = "volume", limit: int = 100):
    if not supabase: return {"status": "error", "message": "DB 설정 안됨"}
    try:
        with market_list_lock:
            data = market_list_cache.get("v")
            if data is None:
                data = _build_market_list()
                market_list_cache["v"] = data
        rows = data["rows"]
        if market.upper() in ("KOSPI", "KOSDAQ"):
            rows = [x for x in rows if x["market"] == market.upper()]
        key = {"volume": "volume", "amount": "amount", "marcap": "marcap_eok", "chg": "chg_pct"}.get(sort, "volume")
        rows = sorted(rows, key=lambda x: (x[key] is None, -(x[key] or 0)))[:max(1, min(limit, 500))]
        return _sanitize_nan({"status": "success", "asof": data["asof"], "total": len(data["rows"]), "data": rows})
    except Exception as e:
        return {"status": "error", "message": str(e)}


def _retry(fn, tries=3):
    """Run fn(); retry with a short back-off. The market page fires 4 requests at once and the host sometimes answers [Errno 11] (resource temporarily unavailable)."""
    for i in range(tries):
        try:
            return fn()
        except Exception:
            if i == tries - 1: raise
            time.sleep(0.4 * (i + 1))


daily_df_cache = TTLCache(maxsize=200, ttl=120)


def _market_daily(symbol):
    """Daily bars of one stock from stock_daily. Unlike quant_core.load_price_from_db a DB error is raised (not turned into an empty frame, which showed as 'no daily data')."""
    if symbol in daily_df_cache: return daily_df_cache[symbol]
    res = _retry(lambda: supabase.table("stock_daily").select("date,open,high,low,close,volume").eq("symbol", symbol).order("date", desc=False).execute())
    if not res.data: return pd.DataFrame()
    df = pd.DataFrame(res.data)
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index().rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"})
    for col in ["Open", "High", "Low", "Close", "Volume"]: df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["Close"])
    if len(df) >= 2: daily_df_cache[symbol] = df
    return df


@app.get("/api/market/chart/{symbol}")
def get_market_chart(symbol: str, tf: str = "D"):
    if not supabase: return {"status": "error", "message": "DB 설정 안됨"}
    if not _SYMBOL_RE.match(symbol): return {"status": "error", "message": "잘못된 종목코드"}
    try:
        df = _market_daily(symbol)
        if df.empty:
            return {"status": "error", "message": "일봉 데이터가 없습니다."}
        if len(df) < 2:
            return {"status": "error", "message": "상장 직후 종목이라 일봉이 1일치뿐입니다. 다음 거래일부터 차트가 표시됩니다."}
        last, prev = float(df["Close"].iloc[-1]), float(df["Close"].iloc[-2])
        tf = tf.upper()
        if tf in ("W", "M", "Y"):
            key = df.index.to_period({"W": "W-FRI", "M": "M", "Y": "Y"}[tf])
            g = df.groupby(key)
            out = g.agg(Open=("Open", "first"), High=("High", "max"), Low=("Low", "min"), Close=("Close", "last"), Volume=("Volume", "sum"))
            out.index = df.index.to_series().groupby(key).last().values
        else:
            out = df[["Open", "High", "Low", "Close", "Volume"]].copy()
        for n in (20, 60, 120):
            out[f"ma{n}"] = out["Close"].rolling(n).mean()
        candles = [{"t": d.strftime("%Y-%m-%d"), "o": r.Open, "h": r.High, "l": r.Low, "c": r.Close, "v": r.Volume, "ma20": r.ma20, "ma60": r.ma60, "ma120": r.ma120} for d, r in zip(out.index, out.itertuples())]
        return _sanitize_nan({"status": "success", "symbol": symbol, "tf": tf, "last": {"close": last, "prev_close": prev, "chg_pct": round((last / prev - 1) * 100, 2) if prev else None}, "candles": candles})
    except Exception as e:
        return {"status": "error", "message": str(e)}


def _norm_opinion(o):
    s = str(o or "").lower().replace(" ", "")
    if "buy" in s or "매수" in s or "비중확대" in s or "outperform" in s: return "매수"
    if "sell" in s or "매도" in s or "비중축소" in s: return "매도"
    if "hold" in s or "중립" in s or "neutral" in s: return "중립"
    return str(o or "-")


def _fetch_naver_targets(symbol):
    import requests as _rq, statistics
    H = {"User-Agent": "Mozilla/5.0"}
    r = _rq.get(f"https://m.stock.naver.com/api/stock/{symbol}/integration", headers=H, timeout=8)
    r.raise_for_status()
    d = json.loads(r.content.decode("utf-8"))
    ci = d.get("consensusInfo") or {}
    mean = int(str(ci.get("priceTargetMean") or "0").replace(",", "") or 0)
    reports = []
    for x in (d.get("researches") or [])[:5]:
        time.sleep(0.4)
        try:
            q = _rq.get(f"https://m.stock.naver.com/api/research/company/{x['id']}", headers=H, timeout=8)
            q.raise_for_status()
            c = json.loads(q.content.decode("utf-8"))["researchContent"]
            goal = int(str(c.get("goalPrice") or "0").replace(",", "") or 0)
            reports.append({"date": c.get("writeDate"), "broker": c.get("brokerName"), "opinion": _norm_opinion(c.get("opinion")), "goal": goal or None, "title": c.get("title")})
        except Exception:
            break      # one failure stops the remaining detail calls (no retry loop)
    goals = [x["goal"] for x in reports if x["goal"]]
    return {"consensus": {"mean": mean or None, "recomm": ci.get("recommMean"), "asof": ci.get("createDate")}, "reports": reports,
            "median_recent": int(statistics.median(goals)) if goals else None, "n_recent": len(goals)}


_kis_tok = {"v": None, "exp": 0.0}


def _kis_token():
    """KIS access token, cached in memory (issuing is limited to 1/min; a token lives 24h)."""
    import requests as _rq
    if _kis_tok["v"] and time.time() < _kis_tok["exp"]: return _kis_tok["v"]
    base = os.environ.get("KIS_BASE_URL", "https://openapi.koreainvestment.com:9443")
    r = _rq.post(f"{base}/oauth2/tokenP", json={"grant_type": "client_credentials", "appkey": os.environ["KIS_APP_KEY"], "appsecret": os.environ["KIS_APP_SECRET"]}, timeout=10)
    tok = r.json()["access_token"]
    _kis_tok.update(v=tok, exp=time.time() + 23 * 3600)
    return tok


def _fetch_kis_targets(symbol):
    """KIS invest-opinion (FHKST663300C0), last 180 days; keep the latest target per broker."""
    import requests as _rq, statistics
    base = os.environ.get("KIS_BASE_URL", "https://openapi.koreainvestment.com:9443")
    end = datetime.now(ZoneInfo("Asia/Seoul")).date()
    H = {"authorization": "Bearer " + _kis_token(), "appkey": os.environ["KIS_APP_KEY"], "appsecret": os.environ["KIS_APP_SECRET"], "tr_id": "FHKST663300C0", "custtype": "P"}
    p = {"FID_COND_MRKT_DIV_CODE": "J", "FID_COND_SCR_DIV_CODE": "16633", "FID_INPUT_ISCD": symbol,
         "FID_INPUT_DATE_1": (end - timedelta(days=180)).strftime("%Y%m%d"), "FID_INPUT_DATE_2": end.strftime("%Y%m%d")}
    r = _rq.get(f"{base}/uapi/domestic-stock/v1/quotations/invest-opinion", headers=H, params=p, timeout=8)
    r.raise_for_status()
    j = r.json()
    if j.get("rt_cd") != "0":
        _kis_tok["exp"] = 0.0      # force a fresh token next time (e.g. expired token)
        raise RuntimeError(f"kis rt_cd={j.get('rt_cd')}")
    latest = {}
    for x in sorted(j.get("output") or [], key=lambda x: x.get("stck_bsop_date", ""), reverse=True):
        goal = int(str(x.get("hts_goal_prc") or "0").replace(",", "") or 0)
        b = x.get("mbcr_name")
        if goal and b and b not in latest:
            d = x["stck_bsop_date"]
            latest[b] = {"date": f"{d[:4]}-{d[4:6]}-{d[6:]}", "broker": b, "opinion": _norm_opinion(x.get("invt_opnn")), "goal": goal, "title": None}
    reports = list(latest.values())
    goals = [x["goal"] for x in reports]
    return {"consensus": {"mean": int(statistics.mean(goals)) if goals else None, "recomm": None, "asof": reports[0]["date"] if reports else None}, "reports": reports,
            "median_recent": int(statistics.median(goals)) if goals else None, "n_recent": len(goals), "source": "kis"}


@app.get("/api/market/targets/{symbol}")
def get_market_targets(symbol: str):
    if not _SYMBOL_RE.match(symbol): return {"status": "error", "message": "잘못된 종목코드"}
    with target_lock:
        if symbol in target_cache: return {"status": "success", "cached": True, **target_cache[symbol]}
        if symbol in target_fail_cache: return {"status": "none", "message": "목표가를 가져오지 못했습니다 (잠시 후 다시 시도됩니다)"}
        try:
            v = None
            if os.environ.get("KIS_APP_KEY") and os.environ.get("KIS_APP_SECRET"):
                try:
                    v = _fetch_kis_targets(symbol)
                except Exception as e:
                    print(f"[targets] {symbol} kis failed, naver fallback: {type(e).__name__}")
                if v is not None and not v["reports"]: v = None      # no KIS rows -> try Naver
            if v is None: v = _fetch_naver_targets(symbol)
            if not v["consensus"]["mean"] and not v["reports"]:
                target_fail_cache[symbol] = True
                return {"status": "none", "message": "이 종목의 목표가 데이터가 없습니다"}
            target_cache[symbol] = v
            return {"status": "success", "cached": False, **v}
        except Exception as e:
            target_fail_cache[symbol] = True
            print(f"[targets] {symbol} failed: {type(e).__name__}")
            return {"status": "none", "message": "목표가를 가져오지 못했습니다 (잠시 후 다시 시도됩니다)"}


news_stock_cache = TTLCache(maxsize=500, ttl=300)
_NAME_OK = re.compile(r"[^0-9A-Za-z가-힣 &.\-]")
sector_cache = TTLCache(maxsize=3000, ttl=86400)
_SECTOR_SPLIT = re.compile(r"[와과및,·/\s]+")
_SECTOR_GENERIC = {"장비", "서비스", "기기", "용품", "제품", "일반", "기타", "관련"}


def _get_sector(symbol: str):
    """(fine, label). fine = stock_fundamental.sector (industry name, used for news keywords); label = fine, else stock_sector.sector (coarse), for display."""
    if symbol in sector_cache: return sector_cache[symbol]
    try:
        r = supabase.table("stock_fundamental").select("sector").eq("symbol", symbol).execute().data
        fine = (r[0].get("sector") if r else None) or None
        label = fine
        if not label:
            r = supabase.table("stock_sector").select("sector").eq("symbol", symbol).execute().data
            label = (r[0].get("sector") if r else None) or None
    except Exception as e:
        print(f"[sector] {symbol} failed: {type(e).__name__}")
        return (None, None)
    sector_cache[symbol] = (fine, label)
    return sector_cache[symbol]


def _sector_tokens(sector):
    toks = [t for t in _SECTOR_SPLIT.split(sector or "") if len(t) >= 2 and t not in _SECTOR_GENERIC]
    return list(dict.fromkeys(toks))


@app.get("/api/market/news/{symbol}")
def get_market_news(symbol: str, name: str = ""):
    """Related news: market_news rows whose title or sector_asset mentions the stock name (name comes from the client; sanitized)."""
    if not supabase: return {"status": "error", "message": "DB 설정 안됨"}
    if not _SYMBOL_RE.match(symbol): return {"status": "error", "message": "잘못된 종목코드"}
    nm = _NAME_OK.sub("", name).strip()[:30]
    if len(nm) < 2: return {"status": "success", "data": []}
    try:
        ck = f"{symbol}:{nm}"
        if ck in news_stock_cache: return {"status": "success", "data": news_stock_cache[ck]}
        cols = "id,title,summary,region,sector_asset,sentiment_score,created_at,is_major"
        r = supabase.table("market_news").select(cols).or_(f"title.ilike.%{nm}%,sector_asset.ilike.%{nm}%").order("created_at", desc=True).limit(30).execute()
        def _clean(t):
            t = re.sub(r"<br\s*/?>", "\n", t or "")
            return re.sub(r"<[^>]+>", "", t).strip()[:3000]
        def _fmt(x, via):
            return {"id": x["id"], "title": x["title"], "summary": _clean(x.get("summary")), "assets": x.get("sector_asset") or "", "score": x.get("sentiment_score") or 0,
                    "region": x.get("region"), "time": (x.get("created_at") or "")[:16].replace("T", " "), "major": bool(x.get("is_major")), "via": via}
        out = [_fmt(x, "name") for x in (r.data or [])]
        # Sector news: sector-name tokens found in the latest 300 articles (title hit = 2, summary/assets hit = 1 per token, keep score >= 2).
        toks = _sector_tokens(_get_sector(symbol)[0])
        if toks:
            seen = {x["id"] for x in out}
            scored = []
            for x in (supabase.table("market_news").select(cols).order("created_at", desc=True).limit(300).execute().data or []):
                if x["id"] in seen: continue
                title = x.get("title") or ""
                rest = (x.get("summary") or "") + " " + (x.get("sector_asset") or "")
                sc = sum(2 for t in toks if t in title) + sum(1 for t in toks if t in rest)
                if sc >= 2: scored.append((sc, x))
            scored.sort(key=lambda p: (p[0], p[1].get("created_at") or ""), reverse=True)
            out += [_fmt(x, "sector") for _, x in scored[:20]]
        news_stock_cache[ck] = out
        return {"status": "success", "data": out}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/api/market/financials/{symbol}")
def get_market_financials(symbol: str):
    """Annual summary (stock_fundamental) + last 8 quarters (stock_fundamental_quarterly). Units: 억원 (as stored)."""
    if not supabase: return {"status": "error", "message": "DB 설정 안됨"}
    if not _SYMBOL_RE.match(symbol): return {"status": "error", "message": "잘못된 종목코드"}
    try:
        f = _retry(lambda: supabase.table("stock_fundamental").select("*").eq("symbol", symbol).execute().data)
        if not f: return {"status": "none", "message": "재무 데이터가 없습니다", "sector": _get_sector(symbol)[1]}
        f = f[0]
        q = _retry(lambda: supabase.table("stock_fundamental_quarterly").select("bsns_year,quarter,revenue_q,op_profit_q,net_income_q,eps_q,roe,debt_ratio,known_from").eq("symbol", symbol).order("bsns_year", desc=True).order("quarter", desc=True).limit(8).execute().data) or []
        keys = ["revenue_cur", "revenue_prev", "op_profit_cur", "op_profit_prev", "net_income_cur", "net_income_prev", "op_margin", "net_margin", "roe", "roa", "debt_ratio", "per", "pbr", "eps_cur", "eps_prev", "dividend_yield", "marcap_억", "sector", "updated_at"]
        return _sanitize_nan({"status": "success", "annual": {k: f.get(k) for k in keys}, "quarters": q, "sector": _get_sector(symbol)[1]})
    except Exception as e:
        return {"status": "error", "message": str(e)}


def _sanitize_nan(obj):
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, dict):
        return {k: _sanitize_nan(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_nan(v) for v in obj]
    return obj


# ==============================================================================
# Reports
#   Stored in quant_screening_cache (no new table): id = CATEGORY_BASE[category] + int(YYYYMMDD),
#   results = JSON text {category, report_date, title, html, received_at}. One report per category and day.
#   ponytail: needs the id column to accept values up to ~2e8 (int4 max is 2.147e9); not verifiable from code.
# ==============================================================================
import hmac
from datetime import date as _date, timezone as _timezone
from fastapi import HTTPException, Header, Query

CATEGORY_BASE = {"morning": 100_000_000}
REPORT_RANGE = 99_999_999
REPORT_MAX_BYTES = 3_000_000
_CATEGORY_RE = re.compile(r"^[a-z_]{1,20}$")
reports_list_cache = TTLCache(maxsize=64, ttl=60)
reports_lock = threading.Lock()


class ReportIn(BaseModel):
    category: str
    report_date: str
    title: str
    html: str


def _report_id(category: str, d: _date) -> int:
    return CATEGORY_BASE[category] + int(d.strftime("%Y%m%d"))


def _parse_report_id(rid: int):
    """Return (category, date) or None if the id is outside the known category ranges."""
    for cat, base in CATEGORY_BASE.items():
        if base <= rid <= base + REPORT_RANGE:
            try:
                s = str(rid - base).zfill(8)
                return cat, _date(int(s[:4]), int(s[4:6]), int(s[6:]))
            except ValueError:
                return None
    return None


@app.post("/api/reports")
def post_report(body: ReportIn, request: Request, x_report_token: Optional[str] = Header(None)):
    expected = os.environ.get("REPORT_INGEST_TOKEN")
    if not expected:
        raise HTTPException(503, "report ingest disabled")
    if not x_report_token or not hmac.compare_digest(x_report_token.encode(), expected.encode()):
        raise HTTPException(401, "invalid token")
    if not supabase:
        raise HTTPException(503, "DB not configured")
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > REPORT_MAX_BYTES + 100_000:
        raise HTTPException(413, "payload too large")
    if body.category not in CATEGORY_BASE or not _CATEGORY_RE.match(body.category):
        raise HTTPException(422, "unknown category")
    try:
        d = _date.fromisoformat(body.report_date)
    except ValueError:
        raise HTTPException(422, "bad report_date")
    if body.report_date != d.isoformat() or d > (datetime.now(KST).date() + timedelta(days=2)):
        raise HTTPException(422, "bad report_date")
    if not (1 <= len(body.title) <= 200):
        raise HTTPException(422, "bad title")
    nbytes = len(body.html.encode("utf-8"))
    if not (1 <= nbytes <= REPORT_MAX_BYTES):
        raise HTTPException(413 if nbytes else 422, "bad html size")
    rid = _report_id(body.category, d)
    now = datetime.now(_timezone.utc).isoformat()
    payload = json.dumps({"category": body.category, "report_date": d.isoformat(), "title": body.title,
                          "html": body.html, "received_at": now})
    try:
        supabase.table("quant_screening_cache").upsert({"id": rid, "results": payload, "updated_at": now}).execute()
    except Exception as e:
        print(f"[reports] upsert failed: {type(e).__name__}")
        raise HTTPException(500, "store failed")
    with reports_lock:
        reports_list_cache.clear()
    return {"status": "success", "id": rid, "category": body.category, "report_date": d.isoformat(), "bytes": nbytes}


@app.get("/api/reports")
def list_reports(category: str = "morning", limit: int = Query(60, ge=1, le=200)):
    if category not in CATEGORY_BASE:
        raise HTTPException(404, "unknown category")
    if not supabase:
        raise HTTPException(503, "DB not configured")
    key = (category, limit)
    with reports_lock:
        hit = reports_list_cache.get(key)
    if hit is not None:
        return hit
    base = CATEGORY_BASE[category]
    r = (supabase.table("quant_screening_cache").select("id, updated_at")
         .gte("id", base).lte("id", base + REPORT_RANGE).order("id", desc=True).limit(limit).execute())
    data = []
    for x in r.data or []:
        p = _parse_report_id(x["id"])
        if p:
            data.append({"id": x["id"], "category": p[0], "report_date": p[1].isoformat(), "updated_at": x.get("updated_at")})
    out = {"status": "success", "data": data}
    with reports_lock:
        reports_list_cache[key] = out
    return out


@app.get("/api/reports/{report_id}")
def get_report(report_id: int):
    p = _parse_report_id(report_id)
    if not p:
        raise HTTPException(404, "not found")
    if not supabase:
        raise HTTPException(503, "DB not configured")
    r = supabase.table("quant_screening_cache").select("results").eq("id", report_id).execute()
    if not r.data:
        raise HTTPException(404, "not found")
    j = json.loads(r.data[0]["results"])
    return {"status": "success", "id": report_id, "category": p[0], "report_date": p[1].isoformat(),
            "title": j.get("title"), "html": j.get("html")}


# ==============================================================================
# Infinite-buying manager (Laor V2.2 / V4.0): router in infinite_api.py, rules in infinite_engine.py, tables in sql/infinite_buy.sql; login in auth_api.py (sql/app_users.sql).
# ==============================================================================
import auth_api, infinite_api
auth_api.init(supabase)
infinite_api.init(supabase)
app.include_router(auth_api.router)
app.include_router(infinite_api.router)


# ==============================================================================
# 📈 [신규] 순수 DB 기반 차트 & 이동평균선 전용 API
# ==============================================================================
@app.get("/api/stock/chart/{symbol}")
def get_stock_chart_data(symbol: str):
    if not supabase:
        return {"status": "error", "message": "DB 설정 안됨"}
    
    try:
        # 1. DB에서 가격 데이터 불러오기 (quant_core의 함수 재사용)
        # 200일 이동평균선을 계산하려면 최소 200일치의 과거 데이터가 필요하므로 넉넉히 가져옵니다.
        df = load_price_from_db(supabase, symbol)
        
        if df.empty or len(df) < 2:
            return {"status": "error", "message": "차트 데이터를 구성할 데이터가 부족합니다."}
        
        # 2. 파이썬(Pandas)으로 이동평균선 완벽하게 계산
        df['ma5'] = df['Close'].rolling(window=5).mean().round(1)
        df['ma20'] = df['Close'].rolling(window=20).mean().round(1)
        df['ma50'] = df['Close'].rolling(window=50).mean().round(1)
        df['ma60'] = df['Close'].rolling(window=60).mean().round(1)
        df['ma120'] = df['Close'].rolling(window=120).mean().round(1)
        df['ma150'] = df['Close'].rolling(window=150).mean().round(1)
        df['ma200'] = df['Close'].rolling(window=200).mean().round(1)
        
        # 3. 화면에 뿌려줄 최근 120일치 데이터만 딱 잘라내기
        df_recent = df.tail(120).copy()
        
        # 4. 프론트엔드가 쓰기 편한 JSON 리스트 형태로 변환
        chart_data = []
        for idx, row in df_recent.iterrows():
            chart_data.append({
                "date": idx.strftime("%Y-%m-%d"),
                "price": float(row['Close']),
                "ma5": float(row['ma5']) if pd.notna(row['ma5']) else None,
                "ma20": float(row['ma20']) if pd.notna(row['ma20']) else None,
                "ma50": float(row['ma50']) if pd.notna(row['ma50']) else None,
                "ma60": float(row['ma60']) if pd.notna(row['ma60']) else None,
                "ma120": float(row['ma120']) if pd.notna(row['ma120']) else None,
                "ma150": float(row['ma150']) if pd.notna(row['ma150']) else None,
                "ma200": float(row['ma200']) if pd.notna(row['ma200']) else None,
            })
            
        return {"status": "success", "data": {"chart_data": chart_data}}
        
    except Exception as e:
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

# [2026-09-17] Render 무료 티어(512MB) OOM 재시작 대응.
# Render API로 메모리 그래프를 직접 확인한 결과, 요청이 쌓일수록(특히 /api/search/{symbol}가
# 매 요청마다 pandas DataFrame을 만들었다 버리는 패턴) 프로세스 RSS가 10~20분 만에
# 450~500MB까지 계단식으로만 올라가고 절대 안 내려갔다 — glibc malloc이 한번 확보한 arena를
# 파이썬 가비지 컬렉션 이후에도 OS에 안 돌려주는 잘 알려진 현상(진짜 객체 누수가 아니라
# "free는 됐는데 OS엔 안 돌려준" 상태). malloc_trim(0)을 주기적으로 강제 호출해 실제로
# 회수시킨다. Linux(Render 컨테이너) 전용 API라 그 외 환경(로컬 Windows/Mac 개발)에서는
# 조용히 스킵한다.
def _malloc_trim():
    try:
        gc.collect()
        libc = ctypes.CDLL("libc.so.6")
        libc.malloc_trim(0)
    except (OSError, AttributeError):
        pass  # Linux glibc가 아닌 환경(Windows 로컬 개발 등) — 조용히 무시

# ==============================================================================
# 🏢 6. 청약 분양 아파트 캘린더 조회
# ==============================================================================
def parse_flexible_date(date_str):
    """'2026.07.31', '2026-07-31', '20260731' 등 다양한 포맷을 date로 파싱"""
    if not date_str:
        return None
    clean = re.sub(r'[^0-9]', '', str(date_str))[:8]
    try:
        return datetime.strptime(clean, '%Y%m%d').date()
    except ValueError:
        return None

@app.get("/api/home/search")
def get_home_calendar(year: int, month: int):
    try:
        start_date = f"{year}-{month:02d}-01"
        end_date = f"{year + 1}-01-01" if month == 12 else f"{year}-{month + 1:02d}-01"
 
        # 1) 청약홈(applyhome) — scheduled_date가 실제 date 컬럼이므로 DB에서 바로 범위 조회
        applyhome_res = (
            supabase.table("housing_listings")
            .select("*")
            .eq("source", "applyhome")
            .gte("scheduled_date", start_date)
            .lt("scheduled_date", end_date)
            .execute()
        )
 
        # 2) LH — closing_date(마감일)가 텍스트 컬럼이라 DB 범위조회 대신 전체를 가져와 파이썬에서 필터링
        lh_res = (
            supabase.table("housing_listings")
            .select("*")
            .eq("source", "lh")
            .execute()
        )
 
        results = []
 
        for row in applyhome_res.data:
            results.append({
                "id": row["id"],
                "name": row["name"],
                "url": row["url"],
                "badge": row["badge"],
                "source": row["source"],
                "date": row["scheduled_date"],
            })
 
        for row in lh_res.data:
            d = parse_flexible_date(row.get("closing_date"))
            if d and d.year == year and d.month == month:
                results.append({
                    "id": row["id"],
                    "name": row["name"],
                    "url": row["url"],
                    "badge": "LH",
                    "source": row["source"],
                    "date": d.isoformat(),
                })
 
        return {"status": "success", "data": results}
 
    except Exception as e:
        return {"status": "error", "message": str(e)}

def _warmup_all():
    refresh_news_cache()
    refresh_quant_cache()

for hour, minute in BATCH_WARMUP_TIMES:
    scheduler.add_job(_warmup_all, "cron", hour=hour, minute=minute)

# 메모리가 512MB 한도까지 올라가는 데 실측상 10~20분 걸렸으므로, 그보다 훨씬 짧은
# 주기로 돌려야 한도 도달 전에 회수할 기회가 여러 번 생긴다.
scheduler.add_job(_malloc_trim, "interval", minutes=3)


def _macro_warm():
    """Rebuild the macro cache in the background (TTL is 300 s) so a visitor never waits for the 2.4 s DB read."""
    try:
        data = _build_macro_full()
        if data:
            with macro_cache_lock:
                macro_cache["v"] = data
    except Exception as e:
        print(f"[macro warm] failed: {type(e).__name__}: {str(e)[:100]}")


def _quant_recheck():
    """Background freshness check of the dashboard cache (the batch writes it once a day): reload only when the DB timestamp changed."""
    try:
        if not supabase: return
        ts = _get_latest_quant_ts()
        if quant_smart_cache.data is None or ts != quant_smart_cache.last_ts:
            refresh_quant_cache()
        else:
            _quant_ts_checked["t"] = time.time()
    except Exception as e:
        print(f"[quant recheck] failed: {type(e).__name__}: {str(e)[:100]}")


scheduler.add_job(_quant_recheck, "interval", seconds=QUANT_TS_RECHECK_SEC)
scheduler.add_job(_macro_warm, "interval", minutes=4, next_run_time=datetime.now(ZoneInfo("Asia/Seoul")) + timedelta(seconds=30))

@app.on_event("startup")
def start_scheduler():
    if supabase:
        threading.Thread(target=_warmup_all, daemon=True).start()
    scheduler.start()

@app.on_event("shutdown")
def stop_scheduler():
    scheduler.shutdown(wait=False)

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
