import os
import json
import threading
import urllib.request
from fastapi import FastAPI
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

# 기존 quant_core 및 real_estate 모듈 활용
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

if not SUPABASE_URL or not SUPABASE_KEY:
    print("⚠️ 경고: Supabase 환경 변수가 설정되지 않았습니다.")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL else None

KST = ZoneInfo("Asia/Seoul")

# ==============================================================================
# 🌟 스마트 캐시 (Last-Modified 기반)
# ==============================================================================
class SmartCache:
    def __init__(self):
        self.lock = threading.Lock()
        self.data = None
        self.last_ts = None

news_smart_cache = SmartCache()
quant_smart_cache = SmartCache()

# 종목 검색용 KRX 리스트 (24시간)
krx_cache = TTLCache(maxsize=5, ttl=86400)

# 💡 개별 종목 펀더멘털 데이터 및 지수 캐시 (50초 유지) - 브라우저 폴링(60초) 대비
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
# ⚡ 4. 실시간 개별 종목/지수 분석 리포트 API (현물 지수 완벽 지원)
# ==============================================================================
@app.get("/api/search/{symbol}")
def search_stock(symbol: str, t: Optional[str] = None):
    if not supabase: return {"status": "error", "message": "DB 설정 안됨"}

    # 1. 1분 TTL 스마트 캐싱
    if symbol in funda_cache:
        return {"status": "success", "data": funda_cache[symbol], "cached": True}

    try:
        is_index = symbol in ['KS11', 'KQ11', 'US500', 'IXIC', 'DJI']
        
        df_price = pd.DataFrame()
        curr_price, prev_close, ret_1d, ret_1m = 0.0, 0.0, 0.0, 0.0
        chart_data = []
        market_status = "장마감"

        if is_index:
            if symbol in ['KS11', 'KQ11']:
                # 🚨 한국 지수: 네이버 실시간 API로 공휴일 완벽 대응 및 정확한 종가 추출
                naver_sym = "KOSPI" if symbol == 'KS11' else "KOSDAQ"
                try:
                    url = f"https://polling.finance.naver.com/api/realtime/domestic/index/{naver_sym}"
                    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                    with urllib.request.urlopen(req, timeout=5) as response:
                        data = json.loads(response.read().decode('utf-8'))
                        ds = data['datas'][0]
                        curr_price = float(ds['closePrice'].replace(',', ''))
                        ret_1d = float(ds['fluctuationsRatio'])
                        m_stat = ds.get('marketStatus', 'CLOSE')
                        market_status = "장중" if m_stat == "OPEN" else "장마감"
                except Exception as e:
                    print("Naver API error:", e)

                # 차트 데이터용 (과거)
                try:
                    df_price = fdr.DataReader(symbol, (now_kst() - timedelta(days=150)).strftime('%Y-%m-%d'))
                    if not df_price.empty:
                        if len(df_price) >= 21:
                            ret_1m = ((curr_price - float(df_price['Close'].iloc[-21])) / float(df_price['Close'].iloc[-21]) * 100)
                        for idx, row in df_price.tail(120).iterrows():
                            chart_data.append({"date": idx.strftime("%Y-%m-%d"), "price": float(row['Close'])})
                except: pass

            else:
                # 🚨 미국 지수: 야후 파이낸스 현물(Spot) 티커로 원복! 본장 열렸을 때만 움직임.
                yahoo_ticker_map = {'US500': '^GSPC', 'IXIC': '^IXIC', 'DJI': '^DJI'}
                y_sym = yahoo_ticker_map.get(symbol, symbol)
                
                # Quote API를 통해 정확한 장중 상태(REGULAR/CLOSED) 파악
                try:
                    quote_url = f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={y_sym}"
                    req = urllib.request.Request(quote_url, headers={'User-Agent': 'Mozilla/5.0'})
                    with urllib.request.urlopen(req, timeout=5) as response:
                        q_data = json.loads(response.read().decode('utf-8'))['quoteResponse']['result'][0]
                        curr_price = float(q_data['regularMarketPrice'])
                        ret_1d = float(q_data['regularMarketChangePercent'])
                        m_stat = q_data.get('marketState', 'CLOSED')
                        market_status = "장중" if m_stat == "REGULAR" else "장마감"
                except Exception as e:
                    print("Yahoo Quote error:", e)

                # Chart API
                try:
                    chart_url = f"https://query1.finance.yahoo.com/v8/finance/chart/{y_sym}?interval=1d&range=150d"
                    req = urllib.request.Request(chart_url, headers={'User-Agent': 'Mozilla/5.0'})
                    with urllib.request.urlopen(req, timeout=5) as response:
                        c_data = json.loads(response.read().decode('utf-8'))['chart']['result'][0]
                        timestamps = c_data.get('timestamp', [])
                        closes = c_data['indicators']['quote'][0].get('close', [])
                        
                        if timestamps and closes:
                            df_price = pd.DataFrame({'Close': closes}, index=pd.to_datetime(timestamps, unit='s', utc=True).tz_convert('Asia/Seoul'))
                            df_price = df_price.dropna()
                            if len(df_price) >= 21:
                                ret_1m = ((curr_price - float(df_price['Close'].iloc[-21])) / float(df_price['Close'].iloc[-21]) * 100)
                            
                            for idx, row in df_price.tail(120).iterrows():
                                chart_data.append({"date": idx.strftime("%Y-%m-%d"), "price": float(row['Close'])})
                except Exception as e:
                    print("Yahoo Chart error:", e)

            # 지수(Index) 결과 포맷팅
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
                "market_status": market_status # 🌟 API를 통한 정확한 휴장/개장 상태
            }
            funda_cache[symbol] = result_data
            return {"status": "success", "data": result_data}

        else:
            # 🌟 일반 종목 처리
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
# 🏢 5. 부동산 아파트 실거래가 스캔 API
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
            gen = generate_excel_data(req.api_key, req.district_code, req.district_name, req.target_dong, sd, ed, filters)
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
```eof

---

### 2. 프론트엔드 수정 (`QuantDesk.jsx`)
제가 임의로 프론트엔드에서 시간을 계산했던 쓰레기 코드를 다 날리고, **API가 넘겨주는 공휴일/주말이 완벽하게 계산된 `indices.kospi.market_status` 값을 사용하도록 교체**했습니다.

```react:quant-frontend/src/pages/QuantDesk.jsx
// ... existing code ...
  const [timeRange, setTimeRange] = useState("All");

  // 🌟 지수 전용 상태 및 모달 제어
  const [indices, setIndices] = useState({ kospi: null, kosdaq: null, nasdaq: null, sp500: null });
  const [isIndexModalOpen, setIsIndexModalOpen] = useState(false);

  // 🌟 공통 API 훅 및 오버레이 가져오기
  const { callApi, ServerWakeupOverlay } = useRenderApi();

  useEffect(() => {
    let intervalId;
    
    const fetchIndices = () => {
      const t = new Date().getTime(); // 캐시 무력화
      Promise.allSettled([
        callApi(`/api/search/KS11?t=${t}`, { background: true }), // KOSPI
        callApi(`/api/search/KQ11?t=${t}`, { background: true }), // KOSDAQ
        callApi(`/api/search/US500?t=${t}`, { background: true }), // S&P 500
        callApi(`/api/search/IXIC?t=${t}`, { background: true })   // NASDAQ
      ]).then((results) => {
        const parse = (res) => res.status === 'fulfilled' && res.value?.status === 'success' ? res.value.data : null;
        setIndices({
          kospi: parse(results[0]),
          kosdaq: parse(results[1]),
          sp500: parse(results[2]),
          nasdaq: parse(results[3])
        });
      });
    };

    // 1. 처음 마운트 될 때 즉시 호출
    fetchIndices(); 

    // 2. 1분(60,000ms)마다 반복 호출 설정
    intervalId = setInterval(fetchIndices, 60000);

    // 3. 컴포넌트가 언마운트되거나 탭이 닫히면 폴링 정리
    return () => clearInterval(intervalId);
  }, [callApi]);

  const fetchQuantData = () => {
// ... existing code ...
          {/* ===================== PORTFOLIO TAB ===================== */}
          {activeTab === "Portfolio" && (
            <div className="animate-in fade-in duration-300 w-full">
                
                {/* 🌟 토스 스타일 지수 티커 */}
                {indices.kospi && (
                  <div 
                    onClick={() => setIsIndexModalOpen(true)}
                    className="mb-8 p-4 md:p-5 bg-white dark:bg-[#0B1120] border border-slate-200 dark:border-slate-800 rounded-2xl shadow-sm flex items-center justify-between cursor-pointer hover:border-blue-400 dark:hover:border-slate-600 transition-all group"
                  >
                    <div className="flex items-center gap-3">
                      <div className="w-8 h-8 rounded-full bg-slate-100 flex items-center justify-center text-lg shadow-inner">🇰🇷</div>
                      <span className="text-[18px] md:text-[20px] font-black text-slate-900 dark:text-white">KOSPI</span>
                      
                      {/* 🌟 API에서 받아온 정확한 장중/장마감 상태값 반영 */}
                      {indices.kospi.market_status === "장중" ? (
                          <span className="text-[10px] font-bold text-emerald-600 bg-emerald-100/50 px-2 py-0.5 rounded-full border border-emerald-200">● 장중</span>
                      ) : (
                          <span className="text-[10px] font-bold text-slate-500 bg-slate-100 dark:bg-slate-800 px-2 py-0.5 rounded-full border border-slate-200 dark:border-slate-700">장마감</span>
                      )}
                    </div>
                    
                    <div className="flex items-center gap-4">
                      <div className="flex flex-col items-end md:flex-row md:items-baseline md:gap-3">
                        <span className="text-[11px] md:text-[13px] font-extrabold text-slate-500 mb-0.5 md:mb-0">
                          전일대비 <span className={(indices.kospi.ret_1d || 0) > 0 ? 'text-[#FF4B4B]' : 'text-[#3B82F6]'}>{(indices.kospi.ret_1d > 0 ? '+' : '')}{indices.kospi.ret_1d?.toFixed(2)}%</span>
                        </span>
                        <span className="text-[22px] md:text-[26px] font-black text-slate-900 dark:text-white tracking-tighter">
                          {indices.kospi.current_price?.toLocaleString('en-US')}
                        </span>
                      </div>
                      <ChevronRight className="text-slate-400 group-hover:text-slate-600 transition-colors" size={20} />
                    </div>
                  </div>
                )}
// ... existing code ...
            {/* List */}
            <div className="p-5 flex flex-col gap-3 bg-slate-50/50 dark:bg-transparent">
              {[
                { key: 'kospi', name: '코스피', icon: 'K', color: 'bg-[#1e4e8c]', isUS: false },
                { key: 'kosdaq', name: '코스닥', icon: 'Q', color: 'bg-[#7e57c2]', isUS: false },
                { key: 'nasdaq', name: 'NASDAQ', icon: 'NDQ', color: 'bg-[#007aff]', isUS: true },
                { key: 'sp500', name: 'S&P 500', icon: 'S&P', color: 'bg-[#ff3b30]', isUS: true }
              ].map(idx => {
                const data = indices[idx.key];
                if (!data) return null;
                const isPos = (data.ret_1d || 0) > 0;
                
                return (
                  <div key={idx.key} className="bg-white dark:bg-[#0B1120] border border-slate-200 dark:border-slate-800 rounded-2xl p-4 flex items-center justify-between shadow-sm">
                    <div className="flex items-center gap-3">
                      <div className={`w-9 h-9 rounded-full ${idx.color} text-white flex items-center justify-center font-black text-[12px] shadow-sm`}>
                        {idx.icon}
                      </div>
                      <div className="flex items-center gap-2">
                        <span className="text-[17px] font-black text-slate-900 dark:text-white">{idx.name}</span>
                        <span className="text-[10px] font-bold text-slate-400 dark:text-slate-500 bg-slate-100 dark:bg-slate-800 px-1.5 py-0.5 rounded border border-slate-200 dark:border-slate-700">지수</span>
                      </div>
                    </div>
                    
                    <div className="flex items-baseline gap-3">
                      <span className={`text-[13px] font-black ${isPos ? 'text-[#FF4B4B]' : 'text-[#3B82F6]'}`}>
                        {isPos ? '+' : ''}{data.ret_1d?.toFixed(2)}%
                      </span>
                      <span className="text-[20px] font-black text-slate-900 dark:text-white tracking-tighter w-24 text-right shrink-0">
                        {data.current_price?.toLocaleString('en-US')}
                      </span>
                    </div>
                  </div>
                );
              })}
            </div>
// ... existing code ...
```eof
