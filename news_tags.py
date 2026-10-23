"""news_tags.py - the fixed keyword list of the news pipeline (Gemini prompts, parser, stock tags) and the regex fallback that tags an article when Gemini gave none.

Tags are stored in market_news.tags (text[]) and in stock_tags.tags; an article reaches a stock when their tag lists overlap.
"""
import re

MARKET_TAGS = ["금리", "물가", "고용", "성장", "환율", "달러", "채권", "유가", "가스", "금", "구리·비철", "관세", "수출통제", "지정학", "증시수급", "공매도", "정책·규제", "중국경제"]
INDUSTRY_TAGS = ["반도체", "AI", "소프트웨어·클라우드", "인터넷·플랫폼", "통신", "디스플레이", "전자·부품", "2차전지", "자동차", "조선", "방산·우주", "원전", "전력·전기장비", "신재생",
                 "정유", "화학", "철강·소재", "건설", "부동산", "기계·로봇", "바이오·제약", "의료기기", "화장품·소비재", "식품·유통", "게임·엔터", "항공·해운·물류", "은행", "증권·보험", "암호화폐"]
ALL_TAGS = MARKET_TAGS + INDUSTRY_TAGS
MAX_TAGS = 5

# the five categories of market_news.region; spellings seen in the table are mapped to them
CATEGORIES = ["거시경제/지수", "외환/채권", "원자재/에너지", "지정학/글로벌", "기업/산업"]
CATEGORY_ALIASES = {"원재료/에너지": "원자재/에너지", "원자재": "원자재/에너지", "에너지": "원자재/에너지", "대체/기타 자산": "원자재/에너지", "거시경제": "거시경제/지수", "지수": "거시경제/지수",
                    "외환": "외환/채권", "채권": "외환/채권", "지정학": "지정학/글로벌", "글로벌": "지정학/글로벌", "기업": "기업/산업", "산업": "기업/산업"}

# spellings Gemini may use for a tag -> the list entry
TAG_ALIASES = {"2차 전지": "2차전지", "배터리": "2차전지", "에이아이": "AI", "인공지능": "AI", "반도체/AI": "반도체", "방산": "방산·우주", "우주항공": "방산·우주", "원자력": "원전",
               "전력": "전력·전기장비", "전기장비": "전력·전기장비", "바이오": "바이오·제약", "제약": "바이오·제약", "항공": "항공·해운·물류", "해운": "항공·해운·물류", "물류": "항공·해운·물류",
               "금융": "은행", "증권": "증권·보험", "보험": "증권·보험", "구리": "구리·비철", "비철금속": "구리·비철", "정책": "정책·규제", "규제": "정책·규제", "경기": "성장", "경제성장": "성장",
               "인플레이션": "물가", "국채": "채권", "원유": "유가", "천연가스": "가스", "소재": "철강·소재", "철강": "철강·소재", "플랫폼": "인터넷·플랫폼", "클라우드": "소프트웨어·클라우드"}

# fallback rules on title + summary (Korean and English terms); a tag needs a hit in the title (x2) or the summary (x1)
_RULES = {
    "금리": r"금리|기준금리|FOMC|연준|Fed\b|ECB|BOJ|일본은행|한국은행|금통위|파월",
    "물가": r"CPI|PPI|PCE|소비자물가|생산자물가|인플레이션|물가",
    "고용": r"고용|비농업|실업수당|실업률|일자리|payroll|jobless",
    "성장": r"GDP|경제성장|ISM|PMI|구매관리자|소매판매|경기침체|경기 둔화|산업생산",
    "환율": r"환율|원화|엔화|위안화|USDKRW|USDJPY|원/달러|엔/달러",
    "달러": r"달러 지수|달러인덱스|DXY|달러 강세|달러 약세",
    "채권": r"국채|채권|입찰|10년물|2년물|국채금리|스프레드",
    "유가": r"유가|WTI|브렌트|OPEC|원유|석유",
    "가스": r"천연가스|LNG|가스 가격",
    "금": r"금 가격|금값|금 선물|금 현물|귀금속|골드",
    "구리·비철": r"구리|비철|알루미늄|니켈|희토류",
    "관세": r"관세|tariff",
    "수출통제": r"수출통제|수출 규제|수출규제|블랙리스트|엔티티 리스트|제재",
    "지정학": r"중동|이스라엘|이란|하마스|러시아|우크라이나|북한|대만|지정학|휴전|전쟁|공습|미사일",
    "증시수급": r"외국인|순매수|순매도|프로그램 매매|수급|MSCI|지수 편입",
    "공매도": r"공매도",
    "정책·규제": r"밸류업|상법|금융위|금감원|공정위|규제 완화|세제 개편|법안",
    "중국경제": r"중국.{0,12}(경제|지표|PMI|CPI|수출|부양|인민은행|위안)|PBOC|인민은행",
    "반도체": r"반도체|HBM|D램|DRAM|낸드|NAND|파운드리|TSMC|엔비디아|하이닉스|ASML|마이크론|Micron|브로드컴|웨이퍼",
    "AI": r"(?<![A-Za-z])AI(?![A-Za-z])|인공지능|OpenAI|Anthropic|챗GPT|LLM|데이터센터|GPU|생성형",
    "소프트웨어·클라우드": r"클라우드|소프트웨어|SaaS|AWS|애저|Azure|마이크로소프트",
    "인터넷·플랫폼": r"플랫폼|네이버|카카오|구글|알파벳|이커머스|검색 광고|아마존",
    "통신": r"통신|5G|SK텔레콤|LG유플러스|이동통신",
    "디스플레이": r"디스플레이|OLED|LCD|패널",
    "전자·부품": r"전자부품|MLCC|카메라 모듈|스마트폰|삼성전기|PCB|기판",
    "2차전지": r"2차전지|배터리|양극재|음극재|전고체|LG에너지|에코프로|CATL|리튬",
    "자동차": r"자동차|전기차|현대차|기아|테슬라|Tesla|완성차|자율주행",
    "조선": r"조선|선박|LNG선|HD현대|한화오션|삼성중공업",
    "방산·우주": r"방산|방위|무기|우주|항공우주|한화에어로|로켓|위성|국방",
    "원전": r"원전|원자력|SMR|두산에너빌리티|우라늄",
    "전력·전기장비": r"전력|변압기|송전|전선|전력망|일렉트릭|효성중공업",
    "신재생": r"태양광|풍력|신재생|재생에너지|수소|ESS",
    "정유": r"정유|정제마진|S-Oil|GS칼텍스|SK이노베이션",
    "화학": r"화학|석유화학|나프타|에틸렌|롯데케미칼|LG화학",
    "철강·소재": r"철강|포스코|POSCO|현대제철|스테인리스",
    "건설": r"건설|시공|착공|분양|GS건설|현대건설|대우건설",
    "부동산": r"부동산|주택|아파트|집값|미분양|리츠",
    "기계·로봇": r"로봇|휴머노이드|자동화|두산로보틱스",
    "바이오·제약": r"바이오|제약|신약|임상시험|임상 시험|임상[123]상|FDA|일라이|비만치료|위고비|셀트리온",
    "의료기기": r"의료기기|임플란트|진단키트",
    "화장품·소비재": r"화장품|뷰티|면세|소비재|아모레|LG생활건강",
    "식품·유통": r"식품|유통|편의점|백화점|이마트|CJ제일제당|라면",
    "게임·엔터": r"게임|엔터|K팝|하이브|JYP|넷마블|크래프톤|엔씨소프트",
    "항공·해운·물류": r"항공|해운|운임|물류|대한항공|HMM|컨테이너",
    "은행": r"(?<!중앙)(?<!한국)(?<!유럽중앙)(?<!일본)(?<!국제결제)(?<!투자)은행|KB금융|신한지주|하나금융|우리금융|예대마진|JP모건|골드만삭스|뱅크오브아메리카|씨티그룹",
    "증권·보험": r"증권|보험|삼성생명|삼성화재|미래에셋|키움|거래대금",
    "암호화폐": r"비트코인|암호화폐|이더리움|가상자산|코인",
}
RULES = {t: re.compile(p) for t, p in _RULES.items()}
assert set(RULES) == set(ALL_TAGS), set(ALL_TAGS) ^ set(RULES)


def normalize_tags(raw):
    """'반도체, AI ,바이오' -> (['반도체', 'AI', '바이오·제약'], unknown). Splits on commas, applies the aliases, keeps list entries only, max MAX_TAGS, no duplicates."""
    out, unknown = [], []
    for piece in re.split(r"[,;/、]", str(raw or "")):
        t = piece.strip().strip("#").strip()
        if not t:
            continue
        t = TAG_ALIASES.get(t, t)
        if t in ALL_TAGS:
            if t not in out:
                out.append(t)
        else:
            unknown.append(t)
    return out[:MAX_TAGS], unknown


def rule_tags(title, summary="", assets=""):
    """Fallback tags from the text: score = 2 per title hit + 1 per hit in summary / assets, best MAX_TAGS.
    A market-wide tag needs one hit anywhere; an industry tag needs a title hit or two hits in the summary / assets (one passing mention is not enough)."""
    title, rest = title or "", f"{summary or ''} {assets or ''}"
    scored = []
    for tag, rx in RULES.items():
        ht, hr = len(rx.findall(title)), len(rx.findall(rest))
        if ht >= 1 or hr >= (1 if tag in MARKET_TAGS else 2):
            scored.append((2 * ht + hr, tag))
    scored.sort(key=lambda p: (-p[0], ALL_TAGS.index(p[1])))
    return [t for _, t in scored[:MAX_TAGS]]


def normalize_category(raw, tags=()):
    """One of CATEGORIES. A known spelling maps directly; otherwise it is guessed from the tags."""
    c = (raw or "").strip()
    if c in CATEGORIES:
        return c
    if c in CATEGORY_ALIASES:
        return CATEGORY_ALIASES[c]
    tg = set(tags)
    if tg & {"금리", "물가", "고용", "성장", "증시수급"}:
        return "거시경제/지수"
    if tg & {"환율", "달러", "채권"}:
        return "외환/채권"
    if tg & {"유가", "가스", "금", "구리·비철"}:
        return "원자재/에너지"
    if tg & {"지정학", "관세", "수출통제"}:
        return "지정학/글로벌"
    return "기업/산업"
