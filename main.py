# main.py
import os
import math
import json
from datetime import datetime, timedelta
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from supabase import create_client, Client
from dotenv import load_dotenv
import uvicorn
import FinanceDataReader as fdr
import asyncio

# 🛡️ 암호화된 국토부 API 키 복호화를 위한 라이브러리 추가
from cryptography.fernet import Fernet

# 기존 quant_core 및 real_estate 모듈 활용
from quant_core import load_price_from_db, fetch_naver_fundamental, calc_quant_metrics, now_kst
from real_estate import generate_excel_data

load_dotenv()
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    # 기존: allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_origins=["*"], # 👈 이렇게 별표(*)로 바꾸면 Vercel에서도 정상적으로 렌더링됩니다!
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# 🔐 --- [ Fernet 복호화 모듈 세팅 - app.py와 100% 동일하게 일치화 ] ---
# 환경 변수에 ENCRYPTION_KEY가 없으면 app.py에 정의된 하드코딩 마스터키를 Fallback으로 사용합니다.
ENCRYPTION_KEY = os.environ.get("ENCRYPTION_KEY", "vS-1_z0qL18r-58lXb0jVwFwJpPZ_X-6N1xG8Zk1w0c=")
cipher_suite = Fernet(ENCRYPTION_KEY.encode())

def decrypt_text(encrypted_text: str) -> str:
    if not encrypted_text:
        return ""
    try:
        # 암호문(gAAAAA...)을 받아 복호화하여 평문 API 키로 리턴합니다.
        return cipher_suite.decrypt(encrypted_text.encode('utf-8')).decode('utf-8')
    except Exception:
        # 이미 평문이거나 복호화에 실패하면 원래 값을 유지합니다.
        return encrypted_text


@app.get("/api/news")
def get_news(limit: int = 500):
    try:
        res = supabase.table("market_news").select("*").order("created_at", desc=True).limit(limit).execute()
        return {"status": "success", "data": res.data}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# 🌟 퀀트 데스크 전체 탭 데이터 통합 제공 API
@app.get("/api/quant-dashboard")
def get_quant_dashboard():
    try:
        data = {"holdings": [], "trades": [], "history": [], "confirmed": [], "watchlist": []}

        # 포트폴리오 및 매도 히스토리 캐시
        r11 = supabase.table("quant_screening_cache").select("results").eq("id", 11).execute()
        r12 = supabase.table("quant_screening_cache").select("results").eq("id", 12).execute()
        r13 = supabase.table("quant_screening_cache").select("results").eq("id", 13).execute()

        # 예비 종목 및 확정 종목 캐시
        r1 = supabase.table("quant_screening_cache").select("results").eq("id", 1).execute()
        r2 = supabase.table("quant_screening_cache").select("results").eq("id", 2).execute()

        if r11.data: data["holdings"] = json.loads(r11.data[0]["results"])
        if r12.data: data["trades"] = json.loads(r12.data[0]["results"])
        if r13.data: data["history"] = json.loads(r13.data[0]["results"])
        if r1.data: data["confirmed"] = json.loads(r1.data[0]["results"])
        if r2.data: data["watchlist"] = json.loads(r2.data[0]["results"])

        return {"status": "success", "data": data}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/api/krx-list")
def get_krx_list():
    try:
        res = supabase.table("quant_screening_cache").select("results").eq("id", 99).execute()
        if res.data: return {"status": "success", "data": json.loads(res.data[0]["results"])}
        return {"status": "success", "data": []}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/api/search/{symbol}")
def search_stock(symbol: str):
    try:
        r1 = supabase.table("quant_screening_cache").select("results").in_("id", [1, 2]).execute()
        all_cached_stocks = []
        if r1.data:
            for row in r1.data: all_cached_stocks.extend(json.loads(row["results"]))
        cached_data = next((item for item in all_cached_stocks if item.get("symbol") == symbol), None)

        df = load_price_from_db(supabase, symbol)
        if df.empty or len(df) < 60:
            df = fdr.DataReader(symbol, (now_kst() - timedelta(days=300)).strftime('%Y-%m-%d'))

        if df is None or df.empty: return {"status": "error", "message": "종목 데이터 없음"}

        curr_price = float(df['Close'].iloc[-1])
        ret_1m = float((curr_price - df['Close'].iloc[-21]) / df['Close'].iloc[-21] * 100) if len(df) >= 21 else 0

        # 🌟 실제 차트를 그리기 위한 과거 120일 데이터 추출
        chart_df = df[['Close']].tail(120).reset_index()
        chart_data = []
        for _, row in chart_df.iterrows():
            # 💡 [핵심 버그 수정] 'Date' 와 'date' 대소문자 차이로 인한 KeyError 완벽 방지
            dt_val = row.get('Date', row.get('date'))
            dt_str = dt_val.strftime('%Y-%m-%d') if hasattr(dt_val, 'strftime') else str(dt_val)[:10]
            chart_data.append({"date": dt_str, "price": float(row['Close'])})

        # 💡 [핵심 방어 로직] KOSPI 지수(KS11)인 경우, 재무제표 긁어오기/퀀트 연산을 생략하고 차트 데이터만 즉시 반환
        if symbol.upper() == "KS11":
            return {"status": "success", "data": {
                "is_cached": False, "symbol": "KS11", "name": "KOSPI", "market": "Index", "sector": "",
                "current_price": curr_price, "ret_1m": ret_1m, "score": 0, "gates": {},
                "fundamental": {}, "chart_data": chart_data
            }}

        if cached_data:
            return {"status": "success", "data": {
                "is_cached": True, "symbol": symbol, "name": cached_data.get("name", ""),
                "market": cached_data.get("market", ""), "sector": cached_data.get("sector", ""),
                "current_price": cached_data.get("current_price", 0), "ret_1m": cached_data.get("ret_1m", 0),
                "score": cached_data.get("factor_score", 0), "gates": cached_data.get("filter_details", {}),
                "fundamental": cached_data, "chart_data": chart_data
            }}

        fund = fetch_naver_fundamental(symbol)
        metrics = calc_quant_metrics(df, fund)

        f_growth = metrics.get("growth_composite", 0) > 0
        f_mdd = metrics.get("mdd", 0) >= metrics.get("dynamic_mdd_limit", -15)
        f_liq = metrics.get("liquidity_20d", 0) >= 50
        ma20, ma60 = metrics.get("ma20", 0), metrics.get("ma60", 0)
        f_trend = (curr_price > ma20) and (ma20 > ma60) if ma60 > 0 else False
        high_60d = metrics.get("high_60d", curr_price)
        f_break = curr_price >= (high_60d * 0.90) if high_60d > 0 else False
        vol_5d, vol_60d = metrics.get("vol_5d", 0), metrics.get("vol_60d", 1)
        f_vol = vol_5d > (vol_60d * 1.5)

        gates = {
            "Growth Composite": {"name": "Growth Composite", "pass": bool(f_growth),
                                 "reason": f"Comp {metrics.get('growth_composite', 0):+.1f}%"},
            "Dynamic MDD": {"name": "Dynamic MDD", "pass": bool(f_mdd), "reason": f"MDD {metrics.get('mdd', 0):.1f}%"},
            "Liquidity": {"name": "Liquidity", "pass": bool(f_liq),
                          "reason": f"{metrics.get('liquidity_20d', 0):.0f}억"},
            "Trend Alignment": {"name": "Trend Alignment", "pass": bool(f_trend),
                                "reason": "Price > 20MA > 60MA" if f_trend else "추세 미달"},
            "Price Breakout": {"name": "Price Breakout", "pass": bool(f_break),
                               "reason": f"고점대비 {(curr_price / high_60d) * 100:.1f}%" if high_60d else "-"},
            "Volume Surge": {"name": "Volume Surge", "pass": bool(f_vol),
                             "reason": f"Vol {vol_5d / vol_60d:.1f}x 급증" if vol_60d else "-"}
        }

        pass_count = sum([1 for g in gates.values() if g['pass']])
        mom = ((curr_price - ma60) / ma60 * 100) if ma60 > 0 else 0
        net_yoy = metrics.get("net_yoy", 0)
        factor_score = min(99.9, max(0, (pass_count / 6 * 50) + min(25, max(0, net_yoy / 5)) + min(25, max(0, mom))))
        clean_fund = {k: (v if not (isinstance(v, float) and (math.isnan(v) or math.isinf(v))) else None) for k, v in
                      fund.items()}

        return {"status": "success", "data": {
            "is_cached": False, "symbol": symbol, "name": "", "market": "KRX", "sector": clean_fund.get("sector", ""),
            "current_price": curr_price, "ret_1m": ret_1m, "score": round(factor_score, 2), "gates": gates,
            "fundamental": clean_fund, "chart_data": chart_data
        }}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# 🌟 부동산 실제 엑셀 생성 연동 (SSE를 활용한 실시간 로그 전송)
@app.get("/api/realestate/build-stream")
async def build_realestate_stream(gu_code: str, gu_name: str, dong: str, start_date: str, end_date: str,
                                  filters: str = ""):
    async def event_generator():
        try:
            # DB에서 부동산 마스터 키 로드
            res = supabase.table("user_api_keys").select("rtms_key").eq("username", "admin").execute()
            if not res.data or not res.data[0].get("rtms_key"):
                yield f"data: {json.dumps({'status': 'error', 'message': 'API 키가 설정되지 않았습니다.'})}\n\n"
                return

            # 🔐 [핵심 수정] DB에서 꺼내온 암호화 상태의 rtms_key를 대칭키 복호화 처리하여 원본 키를 추출합니다.
            encrypted_key = res.data[0]["rtms_key"]
            rtms_key = decrypt_text(encrypted_key)

            s_date = datetime.strptime(start_date, "%Y-%m-%d")
            e_date = datetime.strptime(end_date, "%Y-%m-%d")
            apt_filters = [f.strip() for f in filters.split(',')] if filters.strip() else []

            # 제네레이터를 순회하며 상태와 로그를 프론트엔드로 실시간 스트리밍
            for status, payload in generate_excel_data(rtms_key, gu_code, gu_name, dong, s_date, e_date, apt_filters):
                if status == "progress" or status == "log":
                    # 로그 전송
                    yield f"data: {json.dumps({'status': 'log', 'message': payload})}\n\n"
                    await asyncio.sleep(0.1)  # 버퍼링 방지용 약간의 지연
                elif status == "error":
                    yield f"data: {json.dumps({'status': 'error', 'message': payload})}\n\n"
                    return
                elif status == "success":
                    # 엑셀 바이너리 데이터 임시 저장
                    app.state.last_excel_data = payload["data"]
                    app.state.last_excel_filename = payload["filename"]
                    yield f"data: {json.dumps({'status': 'done', 'message': '엑셀 생성 완료!'})}\n\n"
                    return

        except Exception as e:
            yield f"data: {json.dumps({'status': 'error', 'message': str(e)})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# 엑셀 다운로드 전용 엔드포인트
@app.get("/api/realestate/download")
async def download_realestate():
    if not hasattr(app.state, 'last_excel_data') or not app.state.last_excel_data:
        raise HTTPException(status_code=404, detail="다운로드할 엑셀 데이터가 없습니다.")

    from fastapi.responses import Response
    import urllib.parse
    data = app.state.last_excel_data
    filename = app.state.last_excel_filename

    # 다운로드 후 메모리 비우기
    app.state.last_excel_data = None

    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={urllib.parse.quote(filename)}"}
    )


if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
