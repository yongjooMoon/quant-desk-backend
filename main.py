# ... existing code ...
from supabase import create_client, Client
from dotenv import load_dotenv
import uvicorn
import FinanceDataReader as fdr
import asyncio
from cachetools import TTLCache  # 🌟 캐시 라이브러리 추가

# 기존 quant_core 및 real_estate 모듈 활용
from quant_core import load_price_from_db, fetch_naver_fundamental, calc_quant_metrics, now_kst
from real_estate import generate_excel_data
# ... existing code ...
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://unvcqrjzvtgtjovfyvow.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "sb_publishable_h6pGCCiC9n71So4ZesW4bQ_MNwKlI60")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# 🌟 메모리 캐시 저장소 세팅 (뉴스/퀀트: 10분, KRX리스트: 24시간)
news_cache = TTLCache(maxsize=10, ttl=600)
quant_cache = TTLCache(maxsize=10, ttl=600)
krx_cache = TTLCache(maxsize=10, ttl=86400)

@app.get("/api/news")
def get_news(limit: int = 500, refresh: str = "false"):
    cache_key = f"news_{limit}"
    
    # 💡 사용자가 동기화 버튼을 누른 경우 캐시 강제 삭제
    if refresh.lower() == "true" and cache_key in news_cache:
        del news_cache[cache_key]
        
    if cache_key in news_cache:
        return news_cache[cache_key]

    try:
        res = supabase.table("market_news").select("*").order("created_at", desc=True).limit(limit).execute()
        result = {"status": "success", "data": res.data}
        news_cache[cache_key] = result
        return result
    except Exception as e: 
        return {"status": "error", "message": str(e)}

# 🌟 퀀트 데스크 전체 탭 데이터 통합 제공 API
@app.get("/api/quant-dashboard")
def get_quant_dashboard(refresh: str = "false"):
    cache_key = "quant_dashboard_main"
    
    if refresh.lower() == "true" and cache_key in quant_cache:
        del quant_cache[cache_key]
        
    if cache_key in quant_cache:
        return quant_cache[cache_key]

    try:
        data = {"holdings": [], "trades": [], "history": [], "confirmed": [], "watchlist": []}
# ... existing code ...
        if r1.data: data["confirmed"] = json.loads(r1.data[0]["results"])
        if r2.data: data["watchlist"] = json.loads(r2.data[0]["results"])

        result = {"status": "success", "data": data}
        quant_cache[cache_key] = result
        return result
    except Exception as e: 
        return {"status": "error", "message": str(e)}

@app.get("/api/krx-list")
def get_krx_list(refresh: str = "false"):
    cache_key = "krx_list_main"
    
    if refresh.lower() == "true" and cache_key in krx_cache:
        del krx_cache[cache_key]
        
    if cache_key in krx_cache:
        return krx_cache[cache_key]

    try:
        res = supabase.table("quant_screening_cache").select("results").eq("id", 99).execute()
        if res.data: 
            result = {"status": "success", "data": json.loads(res.data[0]["results"])}
            krx_cache[cache_key] = result
            return result
        return {"status": "success", "data": []}
    except Exception as e: return {"status": "error", "message": str(e)}

@app.get("/api/search/{symbol}")
def search_stock(symbol: str):
# ... existing code ...
