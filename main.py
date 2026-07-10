import os
import json
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from supabase import create_client, Client
from pydantic import BaseModel
from dotenv import load_dotenv
import uvicorn
import FinanceDataReader as fdr
from datetime import datetime, timedelta
import asyncio
from typing import Optional, List
from cachetools import TTLCache, cached  # 🌟 캐시 라이브러리 추가

# 기존 quant_core 및 real_estate 모듈 활용
from quant_core import load_price_from_db, fetch_naver_fundamental, calc_quant_metrics, now_kst
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

if not SUPABASE_URL or not SUPABASE_KEY:
    print("⚠️ 경고: Supabase 환경 변수가 설정되지 않았습니다.")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL else None

# ==============================================================================
# 🌟 인메모리(RAM) 캐시 설정
# TTLCache: 정해진 시간(ttl) 동안 최대 n개(maxsize)의 결과를 서버 메모리에 들고 있습니다.
# ==============================================================================
# 뉴스 & 퀀트 대시보드 데이터: 10분(600초) 캐시
news_cache = TTLCache(maxsize=5, ttl=600)
quant_cache = TTLCache(maxsize=5, ttl=600)

# 종목 검색용 KRX 리스트: 하루(86400초) 캐시 (이건 하루에 한 번만 바뀌면 충분함)
krx_cache = TTLCache(maxsize=5, ttl=86400)


# ==============================================================================
# 📰 1. 마켓 뉴스 데스크 API (10분 캐시 적용)
# ==============================================================================
@app.get("/api/news")
@cached(cache=news_cache) # 💡 사용자가 아무리 클릭해도 10분 내에는 DB 안 가고 RAM에서 즉시 반환
def get_news(limit: int = 500):
    if not supabase: return {"status": "error", "message": "DB 설정 안됨"}
    try:
        res = supabase.table("market_news").select("*").order("created_at", desc=True).limit(limit).execute()
        return {"status": "success", "data": res.data}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ==============================================================================
# 📈 2. 퀀트 데스크 전체 탭 데이터 통합 제공 API (10분 캐시 적용)
# ==============================================================================
@app.get("/api/quant-dashboard")
@cached(cache=quant_cache) # 💡 퀀트 화면 이동 시, 동기화 버튼 클릭 시에도 10분간은 번개처럼 로딩
def get_quant_dashboard():
    if not supabase: return {"status": "error", "message": "DB 설정 안됨"}
    try:
        data = {
            "holdings": [],
            "trades": [],
            "history": [],
            "confirmed": [],
            "watchlist": []
        }

        # 1) 포트폴리오(캐시) 정보
        r1 = supabase.table("quant_screening_cache").select("results").in_("id", [11, 12, 13]).execute()
        if r1.data:
            for row in r1.data:
                try:
                    res_json = json.loads(row["results"])
                    if row["id"] == 11: data["holdings"] = res_json
                    elif row["id"] == 12: data["trades"] = res_json
                    elif row["id"] == 13: data["history"] = res_json
                except: pass
                
        # 2) 스크리닝(캐시) 정보
        r2 = supabase.table("quant_screening_cache").select("results").in_("id", [1, 2]).execute()
        if r2.data:
            for row in r2.data:
                try:
                    res_json = json.loads(row["results"])
                    if row["id"] == 1: data["confirmed"] = res_json
                    elif row["id"] == 2: data["watchlist"] = res_json
                except: pass

        return {"status": "success", "data": data}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ==============================================================================
# 🔍 3. 개별 종목 검색창 목록 제공 API (24시간 캐시 적용)
# ==============================================================================
@app.get("/api/krx-list")
@cached(cache=krx_cache) # 💡 종목 검색 드롭다운은 24시간 내내 DB를 안 거치고 즉각 튀어나옴
def get_krx_list():
    if not supabase: return {"status": "error", "message": "DB 설정 안됨"}
    try:
        res = supabase.table("quant_screening_cache").select("results").eq("id", 99).execute()
        if res.data:
            return {"status": "success", "data": json.loads(res.data[0]["results"])}
        return {"status": "success", "data": []}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ==============================================================================
# ⚡ 4. 실시간 개별 종목 분석 리포트 API (캐시 안 함 - 항상 최신)
# ==============================================================================
@app.get("/api/search/{symbol}")
def search_stock(symbol: str):
    if not supabase: return {"status": "error", "message": "DB 설정 안됨"}
    try:
        df_price = load_price_from_db(supabase, symbol)
        if df_price.empty:
            df_price = fdr.DataReader(symbol, (now_kst() - timedelta(days=300)).strftime('%Y-%m-%d'))
            
        if df_price.empty:
            return {"status": "error", "message": "차트 데이터를 불러올 수 없습니다."}

        # 1. 차트 데이터 정제 (최근 120일)
        chart_data = []
        df_recent = df_price.tail(120)
        for idx, row in df_recent.iterrows():
            chart_data.append({
                "date": idx.strftime("%Y-%m-%d"),
                "price": int(row['Close'])
            })

        curr_price = int(df_price['Close'].iloc[-1])
        ret_1m = ((curr_price - df_price['Close'].iloc[-21]) / df_price['Close'].iloc[-21] * 100) if len(df_price) >= 21 else 0

        # 2. 펀더멘털 및 스코어 (실시간)
        naver_fund = fetch_naver_fundamental(symbol)
        
        from quant_core import evaluate_stock_score
        score, gates = evaluate_stock_score(df_price, naver_fund, curr_price)

        # 3. KOSPI 면 마켓 등 정보 수정
        market = "KOSPI"
        sector = ""
        
        # krx list에서 종목명 등 조회 시도
        r_krx = supabase.table("quant_screening_cache").select("results").eq("id", 99).execute()
        stock_name = symbol
        if r_krx.data:
            krx_list = json.loads(r_krx.data[0]["results"])
            matched = next((item for item in krx_list if item["Symbol"] == symbol), None)
            if matched:
                stock_name = matched.get("Name", symbol)
                # 만약 캐시 내에 마켓, 섹터 정보가 있다면 반영 (현재는 SearchStr 등만 있음)

        result_data = {
            "symbol": symbol,
            "name": stock_name,
            "current_price": curr_price,
            "ret_1m": ret_1m,
            "score": score,
            "gates": gates,
            "fundamental": naver_fund,
            "chart_data": chart_data,
            "market": market,
            "sector": sector
        }

        return {"status": "success", "data": result_data}

    except Exception as e:
        return {"status": "error", "message": str(e)}


# ==============================================================================
# 🏢 5. 부동산 아파트 실거래가 스캔 API (스트리밍 파싱)
# ==============================================================================
class RealEstateRequest(BaseModel):
    api_key: str
    district_code: str
    district_name: str
    target_dong: str
    start_date: str 
    end_date: str
    apt_filters: str

from fastapi.responses import StreamingResponse

@app.post("/api/real-estate")
async def scan_real_estate(req: RealEstateRequest):
    async def event_generator():
        try:
            sd = datetime.strptime(req.start_date, "%Y-%m-%d")
            ed = datetime.strptime(req.end_date, "%Y-%m-%d")
            filters = [f.strip() for f in req.apt_filters.split(',')] if req.apt_filters else []
            
            gen = generate_excel_data(
                req.api_key, req.district_code, req.district_name,
                req.target_dong, sd, ed, filters
            )
            
            for event_type, data in gen:
                if event_type == "success":
                    import base64
                    b64_data = base64.b64encode(data["data"]).decode('utf-8')
                    yield f"data: {json.dumps({'status': 'success', 'file_data': b64_data, 'filename': data['filename']})}\n\n"
                    break
                elif event_type == "error":
                    yield f"data: {json.dumps({'status': 'error', 'message': data})}\n\n"
                    break
                else:
                    yield f"data: {json.dumps({'status': event_type, 'message': data})}\n\n"
                await asyncio.sleep(0.01)
        except Exception as e:
            yield f"data: {json.dumps({'status': 'error', 'message': str(e)})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
