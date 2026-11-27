#!/usr/bin/env python3
"""중수청·공소청 언론보도 텔레그램 알림

30분마다(GitHub Actions) 구글 뉴스(RSS)에서 새 기사를 찾아
요지를 요약하고 링크와 함께 텔레그램으로 보낸다.
"""
import datetime as dt
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import requests

KST = dt.timezone(dt.timedelta(hours=9))

# ── 설정 (환경변수로 바꿀 수 있음) ─────────────────────────────
# 검색어(쉼표 구분). 2026.10.7.~11.3. 중수청 뉴스스크랩 32회분(기사 612건) 분석으로 정함
DEFAULT_QUERIES = ",".join([
    # 기관
    "중수청", "중대범죄수사청", "중수청장", "공소청",
    "서울중수청 OR 서울지방중수청 OR 부산중수청 OR 대구중수청 OR 광주중수청 OR 대전중수청 OR 수원중수청",
    "서울공소청 OR 서울중앙공소청 OR 수원공소청 OR 대구공소청 OR 부산공소청 OR 광주공소청 OR 대전공소청",
    # 인물
    "김지용", "임은정",
    # 제도·쟁점
    "수사 기소 분리", "형사사법체계 개편", "보완수사 요구", "검찰개혁",
    "검찰총장 대행", "수사기관장 국무회의", "법왜곡죄",
    # 유관기관
    "공수처",
])
QUERIES = [q.strip() for q in (os.getenv("QUERIES") or DEFAULT_QUERIES).split(",") if q.strip()]
# ── 네이버 뉴스 검색(NAVER API HUB). 키가 없거나 NAVER_SEARCH=off 이면 구글만 사용 ──
NAVER_ID = (os.getenv("NAVER_APIHUB_KEY_ID") or "").strip()
NAVER_KEY = (os.getenv("NAVER_APIHUB_KEY") or "").strip()
NAVER_ON = ((os.getenv("NAVER_SEARCH") or "on").strip().lower() != "off") and bool(NAVER_ID and NAVER_KEY)
NAVER_URL = "https://naverapihub.apigw.ntruss.com/search/v1/news"
NAVER_DISPLAY = int(os.getenv("NAVER_DISPLAY") or "100")   # 한 번에 받는 건수(최대 100)
_NAVER_FAILS = [0]
BGM_USED = [""]                                     # 이번 브리핑에 쓴 배경음악(기록용)
AUDIO_ERR: list = []                                # 음성 브리핑 중 AI 오류(state.json에 남겨 원인 확인용)                                 # 네이버 호출이 3번 실패하면 이번 실행에서는 더 부르지 않음
DROPPED: list = []                                 # 이번 실행에서 거른 기사 [시각, 이유, 언론사, 제목] → state.json에 보관
def _naver_queries() -> list:
    raw = os.getenv("NAVER_QUERIES")
    qs = [q.strip() for q in raw.split(",") if q.strip()] if raw else \
        [x.strip() for q in QUERIES for x in re.split(r"\s+OR\s+", q) if x.strip()]
    return list(dict.fromkeys(qs))
NAVER_QUERIES = _naver_queries()
PRIORITY_TERMS = ("중수청", "중대범죄수사청", "김지용", "임은정")   # 이 단어가 제목에 있으면 맨 위에
SEEN_FILE = os.getenv("SEEN_FILE", "seen.json")    # 이미 보낸 기사 기록
STATE_FILE = os.getenv("STATE_FILE", "state.json") # 공소청·형사사법 모음 대기 목록
DIGEST_MINUTES = int(os.getenv("DIGEST_MINUTES") or "120")   # 공소청·형사사법 기사 모음 간격(분) — DIGEST_TIMES=off일 때만
# 공소청·형사사법 모음을 보내는 시각(쉼표 구분). off면 DIGEST_MINUTES 간격으로
DIGEST_TIMES = (os.getenv("DIGEST_TIMES") or "09:00,13:00,18:00").strip()
# 모음 모양: page(기본, 텔레그래프 웹페이지 한 장 + 대화방엔 링크 한 줄) / full(요약까지 펼침) / list(제목 한 줄씩) / fold(접기 — 누르면 다시 접혀 불편)
DIGEST_STYLE = (os.getenv("DIGEST_STYLE") or "page").strip().lower()
DIGEST_TEST = (os.getenv("DIGEST_TEST") or "").strip().lower() == "true"   # Actions 수동 시험: 대기 중인 모음을 지금 보냄
LEAD_TERMS = ("중수청", "중대범죄수사청")             # 본문 첫머리에 있으면 중수청 기사로 봄
KEEP_DAYS = 7                                      # 기록 보관 기간
MAX_AGE_HOURS = 24                                 # 이보다 오래된 기사는 무시(최대)
BUFFER_HOURS = 2                                   # 직전 실행보다 이만큼 앞선 기사까지 받음(포털 반영 지연 대비)
FIRST_RUN_HOURS = 2                                # 첫 실행 때는 최근 2시간만
BATCH = int(os.getenv("BATCH", "12"))              # AI에 한 번에 보내는 기사 수(많으면 나눠서 처리, 버리는 기사 없음)
QUIET_HOURS = os.getenv("QUIET_HOURS", "")         # 예: "0-6" → 0~6시엔 보내지 않고 모아뒀다가 7시에 발송
# 받지 않을 언론사(쉼표 구분). 언론사 이름이나 주소 일부가 들어가면 제외
EXCLUDE_PRESS = [p.strip() for p in (os.getenv("EXCLUDE_PRESS") or "중수청일보").split(",") if p.strip()]

# ── 받을 언론사 (중수청 뉴스스크랩 33회분 분석: 1·2군 41개사 + 지방지) ──
# PRESS_FILTER=off 로 설정하면 언론사 제한 없이 모두 받음
PRESS_FILTER = os.getenv("PRESS_FILTER", "on").strip().lower() != "off"
ALLOWED_PRESS = {
    # 1군: 석간
    "문화일보": "munhwa.com", "내일신문": "naeil.com", "아시아경제": "asiae.co.kr", "헤럴드경제": "heraldcorp.com",
    # 1군: 종합 일간지
    "조선일보": "chosun.com", "서울신문": "seoul.co.kr", "경향신문": "khan.co.kr", "세계일보": "segye.com",
    "중앙일보": "joongang.co.kr", "국민일보": "kmib.co.kr", "한국일보": "hankookilbo.com", "한겨레": "hani.co.kr",
    # 1군: 통신사
    "뉴스1": "news1.kr", "뉴시스": "newsis.com", "연합뉴스": "yna.co.kr",
    # 1군: 경제지
    "서울경제": "sedaily.com", "매일경제": "mk.co.kr", "파이낸셜뉴스": "fnnews.com", "한국경제": "hankyung.com",
    "아시아투데이": "asiatoday.co.kr",
    # 1군: 방송
    "KBS": "kbs.co.kr", "JTBC": "jtbc.co.kr", "연합뉴스TV": "yonhapnewstv.co.kr", "MBN": "mbn.co.kr",
    "SBS": "sbs.co.kr", "MBC": "imbc.com",
    # 2군
    "동아일보": "donga.com", "TV조선": "tvchosun.com", "YTN": "ytn.co.kr", "아주경제": "ajunews.com",
    "이데일리": "edaily.co.kr", "머니투데이": "mt.co.kr", "뉴스토마토": "newstomato.com", "매일신문": "imaeil.com",
    "노컷뉴스": "nocutnews.co.kr", "대구신문": "idaegu.co.kr", "더팩트": "tf.co.kr", "법률신문": "lawtimes.co.kr",
    "뉴스핌": "newspim.com", "경상매일신문": "ksmnews.co.kr",
    # 추가 요청
    "오마이뉴스": "ohmynews.com", "데일리안": "dailian.co.kr", "채널A": "ichannela.com", "시사저널": "sisajournal.com",
    # 11.5. 조간 스크랩 반영
    "프레시안": "pressian.com", "천지일보": "newscj.com", "조선비즈": "biz.chosun.com", "호남신문": "honamnews.co.kr",
    # 지방지
    "부산일보": "busan.com", "국제신문": "kookje.co.kr", "영남일보": "yeongnam.com", "대구일보": "idaegu.com",
    "경북일보": "kyongbuk.co.kr", "경북신문": "kbsm.net", "경북매일": "kbmaeil.com", "대경일보": "dkilbo.com",
    "일간경북신문": "", "경북연합일보": "", "대구광역일보": "", "경남신문": "knnews.co.kr", "경남도민일보": "idomin.com",
    "경상일보": "ksilbo.co.kr", "울산매일": "iusm.co.kr", "광주일보": "kwangju.co.kr", "광주매일신문": "kjdaily.com",
    "남도일보": "namdonews.com", "무등일보": "mdilbo.com", "전남일보": "jnilbo.com", "전북일보": "jjan.kr",
    "전북도민일보": "domin.co.kr", "새전북신문": "sjbnews.com", "대전일보": "daejonilbo.com", "중도일보": "joongdo.co.kr",
    "충청투데이": "cctoday.co.kr", "충남일보": "chungnamilbo.co.kr", "충북일보": "inews365.com", "충청매일": "ccdn.co.kr",
    "충청타임즈": "cctimes.kr", "중부매일": "jbnews.com", "충청일보": "ccdailynews.com", "경인일보": "kyeongin.com",
    "경기일보": "kyeonggi.com", "중부일보": "joongboo.com", "기호일보": "kihoilbo.co.kr", "인천일보": "incheonilbo.com",
    "강원일보": "kwnews.co.kr", "강원도민일보": "kado.net", "제주일보": "jejunews.com", "한라일보": "ihalla.com",
    "제민일보": "jemin.com", "산경일보": "", "동방일보": "", "대한일보": "",
    # 11.12. 스크랩 대조 후 추가(도메인 미확인은 이름만 등록 — 구글 표기와 일치해야 함)
    "울산신문": "ulsanpress.net", "전라일보": "jeollailbo.com", "광주타임스": "", "JCN울산중앙방송": "", "알쓸신문": "",
    # 11.26. 스크랩(11.13.~11.26.) 대조 후 추가
    "이투데이": "etoday.co.kr", "경기신문": "kgnews.co.kr", "신아일보": "shinailbo.co.kr", "더퍼블릭": "thepublic.kr",
    "투데이코리아": "todaykorea.co.kr", "대구MBC": "dgmbc.com", "전국매일": "jeonmae.co.kr", "경기매일": "kgmaeil.net",
    "삼다일보": "", "서울일보": "", "드림투데이": "",
}
# 이름이 다르게 표시되는 경우(구글 뉴스 표기)
PRESS_ALIASES = {"서울Pn": "서울신문", "JoongAng": "중앙일보", "Yonhap": "연합뉴스", "TV조선뉴스": "TV조선",
                 "news1": "뉴스1", "NEWSIS": "뉴시스", "한겨레신문": "한겨레", "The Fact": "더팩트",
                 "OhmyNews": "오마이뉴스", "ChosunBiz": "조선비즈", "Chosunbiz": "조선비즈", "뉴스천지": "천지일보", "오마이TV": "오마이뉴스", "Channel A": "채널A", "채널A 뉴스": "채널A"}
BLOCKED_HOSTS = ("weekly.chosun.com", "biz.sbs.co.kr")   # 같은 주소 아래 다른 매체


def press_allowed(press: str, url: str) -> bool:
    if not PRESS_FILTER:
        return True
    host = urlparse(url).netloc.lower()
    if any(host == b or host.endswith("." + b) for b in BLOCKED_HOSTS):
        return False
    name = PRESS_ALIASES.get(press.strip(), press.strip())
    for p, dom in ALLOWED_PRESS.items():
        if name == p or name.startswith(p + " ") or (p in ("KBS", "SBS", "MBC", "MBN", "JTBC", "YTN") and name.startswith(p)):
            return True
        if dom and (host == dom or host.endswith("." + dom)):
            return True
    return False
# 제목·본문에 이 단어가 있으면 제외(쉼표 구분). 예: 동명이인 기사
EXCLUDE_WORDS = [w.strip() for w in (os.getenv("EXCLUDE_WORDS") or "촬영감독,촬영 감독").split(",") if w.strip()]
# 본문에 이 단어가 하나도 없으면 관련 없는 기사로 보고 제외 (동명이인 등 걸러내기)
# AI 판단을 못 쓸 때는 이 단어가 제목·본문에 있는 기사만 보냄(잡음 방지)
CORE_TERMS = ("중수청", "중대범죄수사청", "공소청", "수사·기소 분리", "수사-기소 분리", "수사 기소 분리",
              "수사·기소분리", "형사사법체계", "형사사법 체계", "형사사법 개편", "형사사법개편",
              "보완수사", "검찰청 폐지")
CONTEXT_TERMS = ("중수청", "중대범죄수사청", "공소청", "검찰", "검사", "수사", "청장", "공수처",
                 "경찰", "특검", "형사", "법무", "기소", "사법")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-5-5")
VERIFY_ON = (os.getenv("VERIFY") or "on").strip().lower() != "off"
VERIFY_MODEL = (os.getenv("VERIFY_MODEL") or "").strip() or "claude-sonnet-5-5"   # 검증만 상위 모델(생성은 CLAUDE_MODEL=Haiku)

# ── 음성 브리핑(중수청 기사만, 평일 아침·오후) ──────────────────
# AUDIO=off 로 끄기. AUDIO_TIMES: 만드는 시각(쉼표 구분). 직전 브리핑 이후 보낸 중수청 기사를 담음
AUDIO_ON = (os.getenv("AUDIO") or "on").strip().lower() != "off"
AUDIO_TIMES = os.getenv("AUDIO_TIMES") or "07:10,17:00"
AUDIO_MAX = int(os.getenv("AUDIO_MAX") or "10")          # 한 번에 읽을 최대 사안 수
AUDIO_VOICE = (os.getenv("AUDIO_VOICE") or "").strip()  # 비우면 기본 목소리
AUDIO_TEST = (os.getenv("AUDIO_TEST") or "").strip().lower() == "true"
# Claude가 직접 쓴 원고(briefs/*.json)로 음성·카드만 만들어 보냄(11.27.~). 자동 브리핑은 정시+BRIEF_FALLBACK_MIN분까지
# Claude 원고가 안 오면 대신 나감(대비책)
BRIEF_FILE = (os.getenv("BRIEF_FILE") or "").strip()
BRIEF_FALLBACK_MIN = int(os.getenv("BRIEF_FALLBACK_MIN") or "40")   # Actions에서 수동 시험
GOOGLE_TTS_KEY = (os.getenv("GOOGLE_TTS_KEY") or "").strip()            # 있으면 구글 음성 사용
AUDIO_BGM = (os.getenv("AUDIO_BGM") or "on").strip().lower() != "off"   # 배경음악(직접 합성한 음악이라 저작권 문제 없음)
AUDIO_BGM_STYLE = int(os.getenv("AUDIO_BGM_STYLE") or "1")              # 1 속보 시그널(기본) ~ 5
# 외부 무료 음원 쓰기: AUDIO_BGM_URL(쉼표로 여러 개 → 브리핑마다 돌아가며) 또는 저장소 bgm 폴더의 mp3
# 저장소가 공개라 음원 파일을 올리면 남이 내려받을 수 있음 → 라이선스상 재배포 금지 음원은 URL 방식으로
AUDIO_BGM_URL = os.getenv("AUDIO_BGM_URL") or ""
AUDIO_RATE = float(os.getenv("AUDIO_RATE") or "0.95")    # 읽는 속도(1.0 보통, 0.9 느리게)
# 발음 사전: '바꿀말=읽을말'을 쉼표·줄바꿈으로 구분. 예) NDFAS=엔디파스, 이드러그=이 드러그
AUDIO_DICT = os.getenv("AUDIO_DICT") or ""

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
# 기본 사실표(facts.md): 요약·대본·카드·검증 AI가 모두 참고. '#' 줄은 설명이라 빼고 넘김
try:
    FACTS = "\n".join(l for l in open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "facts.md"),
                                       encoding="utf-8").read().splitlines() if l.strip() and not l.lstrip().startswith("#"))[:3000]
except Exception:
    FACTS = "- 중수청과 공소청은 2026년 10월 2일 출범했다. 중수청은 행정안전부 소속이다."
# 브리핑·카드를 보내기 전 원문 대조 검증(VERIFY=off로 끄기). VERIFY_MODEL로 검증에만 다른 모델 사용 가능   # 없으면 기사 첫머리로 대체

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}

PRESS = {
    "yna.co.kr": "연합뉴스", "yonhapnewstv.co.kr": "연합뉴스TV", "chosun.com": "조선일보",
    "joongang.co.kr": "중앙일보", "donga.com": "동아일보", "hani.co.kr": "한겨레",
    "khan.co.kr": "경향신문", "hankookilbo.com": "한국일보", "seoul.co.kr": "서울신문",
    "kmib.co.kr": "국민일보", "segye.com": "세계일보", "munhwa.com": "문화일보",
    "news1.kr": "뉴스1", "newsis.com": "뉴시스", "kbs.co.kr": "KBS", "imbc.com": "MBC",
    "sbs.co.kr": "SBS", "jtbc.co.kr": "JTBC", "ytn.co.kr": "YTN", "mbn.co.kr": "MBN",
    "ichannela.com": "채널A", "tvchosun.com": "TV조선", "lawtimes.co.kr": "법률신문",
    "mk.co.kr": "매일경제", "hankyung.com": "한국경제", "edaily.co.kr": "이데일리",
    "mt.co.kr": "머니투데이", "sedaily.com": "서울경제", "heraldcorp.com": "헤럴드경제",
    "nocutnews.co.kr": "노컷뉴스", "ohmynews.com": "오마이뉴스", "pressian.com": "프레시안",
    "naver.com": "네이버뉴스", "lawissue.co.kr": "로이슈", "lawleader.co.kr": "법률저널",
}


# ── 유틸 ─────────────────────────────────────────────────────
def clean(text: str) -> str:
    # 네이버는 검색어 강조용 <b> 태그만 붙이고 나머지는 &lt; 등으로 이스케이프함
    return html.unescape(re.sub(r"</?b>", "", text or "")).strip()


def press_name(x: str) -> str:
    """기사 주소('https://www.newsis.com/...') 또는 주소로 들어온 언론사('cctoday.co.kr') → 언론사 이름.
    이미 이름이면 그대로. 끝내 모르면 주소 대신 도메인만(전체 주소가 언론사 자리에 나오지 않게)"""
    x = (x or "").strip()
    if "." not in x or " " in x:
        return x
    host = (urlparse(x).netloc if "://" in x else x.split("/")[0]).lower().removeprefix("www.").removeprefix("m.")
    for domain, name in PRESS.items():
        if host == domain or host.endswith("." + domain):
            return name
    for name, dom in ALLOWED_PRESS.items():
        if dom and (host == dom or host.endswith("." + dom)):
            return name
    return host


def norm_title(t: str) -> str:
    return re.sub(r"[\W_]+", "", re.sub(r"\[[^\]]*\]", "", t)).lower()


SCOOP_RE = re.compile(r"^\s*(?:[\[\(【〈<][^\]\)】〉>]{0,10}단독[^\]\)】〉>]{0,10}[\]\)】〉>]|단독\s*[:：\-·])")


def is_scoop(title: str) -> bool:
    """제목 맨 앞에 [단독]·(단독)·[속보·단독] 같은 표시가 붙은 기사 → 같은 사안이어도 중복으로 거르지 않음"""
    return bool(SCOOP_RE.match(title or ""))


def drop(reason: str, press: str, title: str, now: dt.datetime) -> None:
    """걸러낸 기사와 이유를 기록(state.json의 dropped, 나중에 '왜 안 왔나' 확인용)"""
    DROPPED.append([now.isoformat(), reason, press or "", (title or "")[:90]])


def in_quiet_hours(now: dt.datetime) -> bool:
    m = re.fullmatch(r"\s*(\d{1,2})\s*-\s*(\d{1,2})\s*", QUIET_HOURS)
    if not m:
        return False
    start, end = int(m.group(1)), int(m.group(2))
    h = now.hour
    return start <= h <= end if start <= end else (h >= start or h <= end)


# ── 기록 ─────────────────────────────────────────────────────
def load_seen() -> dict:
    try:
        with open(SEEN_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_seen(seen: dict, now: dt.datetime) -> None:
    cutoff = now - dt.timedelta(days=KEEP_DAYS)
    kept = {k: v for k, v in seen.items() if dt.datetime.fromisoformat(v) >= cutoff}
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(kept, f, ensure_ascii=False, indent=0, sort_keys=True)


def load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        st = {}
    st.setdefault("pending", [])
    st.setdefault("last_digest", "")
    return st


def save_state(st: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


# ── 수집 ─────────────────────────────────────────────────────
def google_search(query: str) -> list:
    """구글 뉴스 RSS 최신 기사 (최근 1일)"""
    r = requests.get(
        "https://news.google.com/rss/search",
        params={"q": f"{query} when:1d", "hl": "ko", "gl": "KR", "ceid": "KR:ko"},
        headers=UA, timeout=15,
    )
    r.raise_for_status()
    items = []
    for it in ET.fromstring(r.content).iter("item"):
        src = it.find("source")
        items.append({
            "title": it.findtext("title", ""),
            "link": it.findtext("link", ""),
            "guid": it.findtext("guid", "") or it.findtext("link", ""),
            "pubDate": it.findtext("pubDate", ""),
            "source": (src.text or "").strip() if src is not None else "",
            "source_url": src.get("url", "") if src is not None else "",
        })
    return items


def naver_search(query: str) -> list:
    """네이버 뉴스 검색(최신순) → 구글 결과와 같은 형식의 목록"""
    if _NAVER_FAILS[0] >= 3:
        return []
    r = None
    try:
        r = requests.get(
            NAVER_URL,
            params={"query": query, "display": NAVER_DISPLAY, "start": 1, "sort": "date"},
            headers={"X-NCP-APIGW-API-KEY-ID": NAVER_ID, "X-NCP-APIGW-API-KEY": NAVER_KEY,
                     "Accept": "application/json"},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        _NAVER_FAILS[0] += 1
        detail = ""
        if r is not None:
            detail = f" (응답 {r.status_code}: {r.text[:150]!r})"
        print(f"[경고] 네이버 검색 실패 {_NAVER_FAILS[0]}/3{detail}: {e}", file=sys.stderr)
        if _NAVER_FAILS[0] >= 3:
            print("[경고] 네이버 검색이 계속 실패해 이번 실행에서는 구글만 사용합니다. "
                  "(키·한도·API HUB 신청 상태 확인)", file=sys.stderr)
        raise
    items = []
    for it in data.get("items", []) if isinstance(data, dict) else []:
        orig = (it.get("originallink") or "").strip()
        link = orig or (it.get("link") or "").strip()
        if not link:
            continue
        items.append({
            "title": clean(it.get("title", "")),
            "link": link,
            "guid": "nv:" + link,
            "pubDate": it.get("pubDate", ""),
            "source": press_name(link),
            "source_url": link,
        })
    return items


def decode_links(arts: list) -> None:
    """구글 뉴스 중계 링크를 실제 언론사 주소로 변환 (실패하면 구글 링크 유지)"""
    arts = [a for a in arts if "news.google.com" in a["url"]]   # 네이버 결과는 이미 원문 주소
    if not arts:
        return
    try:
        from googlenewsdecoder import gnewsdecoder
        results = gnewsdecoder([a["url"] for a in arts], timeout=15)
    except Exception as e:
        print(f"[경고] 링크 변환 실패, 구글 링크 사용: {e}", file=sys.stderr)
        return
    for a, res in zip(arts, results):
        if isinstance(res, dict) and res.get("success") and res.get("decoded_url"):
            a["url"] = res["decoded_url"]
            if not a["press"]:
                a["press"] = press_name(a["url"])


def collect(seen: dict, now: dt.datetime, last_run: str = "") -> list:
    first_run = not seen
    if first_run:
        max_age = dt.timedelta(hours=FIRST_RUN_HOURS)
    elif last_run:
        # 직전 실행 시각 - 2시간 이후 기사만 (밤사이 쉬었다면 그만큼 넓어짐), 최대 24시간
        gap = now - dt.datetime.fromisoformat(last_run) + dt.timedelta(hours=BUFFER_HOURS)
        max_age = min(gap, dt.timedelta(hours=MAX_AGE_HOURS))
    else:
        max_age = dt.timedelta(hours=MAX_AGE_HOURS)
    found, titles = {}, set()
    sources = [(google_search, q) for q in QUERIES]
    if NAVER_ON:
        sources += [(naver_search, q) for q in NAVER_QUERIES]
    for fn, q in sources:
        try:
            items = fn(q)
        except Exception as e:
            if fn is google_search:
                print(f"[경고] '{q}' 검색 실패: {e}", file=sys.stderr)
            continue
        for it in items:
            key = it["guid"]
            if not key or key in seen or key in found:
                continue
            try:
                pub = parsedate_to_datetime(it["pubDate"]).astimezone(KST)
            except (TypeError, ValueError):
                pub = now
            if now - pub > max_age:
                if not key.startswith("nv:"):
                    seen[key] = now.isoformat()    # 지난 기사는 이후에도 보내지 않음(네이버는 기록 안 함: 매번 걸러져 용량만 커짐)
                continue
            press = it["source"]
            title = html.unescape(it["title"]).strip()
            if (any(x in press or x in it["source_url"] or title.endswith(x) for x in EXCLUDE_PRESS)
                    or not press_allowed(press, it["source_url"])):
                if not key.startswith("nv:"):
                    seen[key] = now.isoformat()    # 제외 언론사
                if any(t in title for t in CORE_TERMS + PRIORITY_TERMS):
                    drop(f"언론사 제외({press_name(it['source_url']) or press})", press or press_name(it["source_url"]), title, now)
                continue
            if press and title.endswith(" - " + press):   # 구글 뉴스 제목 끝의 ' - 언론사' 제거
                title = title[: -len(press) - 3].strip()
            if press and title.endswith(":" + press):     # 제목 끝 ':언론사' 제거
                title = title[: -len(press) - 1].strip()
            nt = norm_title(title)
            if nt in titles or ("t:" + nt) in seen:  # 같은 제목 중복 제거(이전 실행에서 처리한 제목 포함)
                if not key.startswith("nv:"):
                    seen[key] = now.isoformat()
                continue
            titles.add(nt)
            found[key] = {
                "key": key,
                "url": it["link"],
                "title": title,
                "pub": pub,
                "press": press or press_name(it["source_url"]),
                "priority": any(t in title for t in PRIORITY_TERMS),
            }
    n_nv = sum(1 for k in found if k.startswith("nv:"))
    print(f"검색 후보 {len(found)}건 (구글 {len(found) - n_nv}건 · 네이버 {n_nv}건)"
          + ("" if NAVER_ON else " · 네이버 검색 꺼짐"))
    arts = sorted(found.values(), key=lambda a: (not a["priority"], -a["pub"].timestamp()))
    return arts


# ── 요약 ─────────────────────────────────────────────────────
def _html_body(html_: str) -> str:
    """trafilatura가 못 뽑는 페이지(자바스크립트로 그리는 조선일보 등)에서 본문 찾기:
    JSON-LD articleBody → 조선일보(Fusion) content_elements → og:description 순"""
    for m in re.finditer(r'"articleBody"\s*:\s*"((?:[^"\\]|\\.){200,})"', html_):
        try:
            return json.loads('"' + m.group(1) + '"')
        except Exception:
            pass
    m = re.search(r"Fusion\.globalContent\s*=\s*(\{.*?\});\s*Fusion\.", html_, re.S)
    if m:
        try:
            g = json.loads(m.group(1))
            t = " ".join(re.sub(r"<[^>]+>", "", c.get("content", "")) for c in g.get("content_elements", [])
                         if c.get("type") == "text")
            if len(t) > 200:
                return t
        except Exception:
            pass
    m = re.search(r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']{60,})', html_)
    return html.unescape(m.group(1)) if m else ""


def _naver_copy(title: str) -> str:
    """본문을 못 읽은 기사는 네이버 뉴스에 실린 같은 기사(n.news.naver.com)에서 본문을 가져옴"""
    if not (NAVER_ID and NAVER_KEY):
        return ""
    q = re.sub(r"\[[^\]]*\]|[\"'“”‘’…]", " ", title).strip()[:60]
    try:
        r = requests.get(NAVER_URL, params={"query": q, "display": 5, "start": 1, "sort": "sim"},
                         headers={"X-NCP-APIGW-API-KEY-ID": NAVER_ID, "X-NCP-APIGW-API-KEY": NAVER_KEY,
                                  "Accept": "application/json"}, timeout=10)
        items = r.json().get("items", []) if r.ok else []
    except Exception:
        return ""
    for it in items:
        link = it.get("link") or ""
        t = re.sub(r"<[^>]+>", "", html.unescape(it.get("title") or ""))
        if "n.news.naver.com" not in link or not similar(t, title, 0.6):
            continue
        try:
            rr = requests.get(link, headers=UA, timeout=10)
            m = re.search(r'id="dic_area"[^>]*>(.*?)</article>', rr.text, re.S) or re.search(r'id="dic_area"[^>]*>(.*?)</div>', rr.text, re.S)
            text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", m.group(1)))).strip() if m else ""
            if len(text) > 200:
                return text
        except Exception:
            continue
    return ""


def fetch_body(art: dict) -> str:
    """기사 본문 추출(실패하면 빈 문자열). 11.27. 조선일보·뉴스1 본문이 비어 카드 요점이 한 줄만 나와
    페이지 속 데이터(JSON-LD 등)와 네이버 뉴스 사본을 차례로 시도"""
    try:
        import trafilatura
    except ImportError:
        return ""
    url, page = art["url"], ""
    if url and "news.google.com" not in url:
        try:
            r = requests.get(url, headers=UA, timeout=10)
            r.encoding = r.apparent_encoding or r.encoding
            page = r.text
            text = trafilatura.extract(page) or ""
            if re.search(r"Access Denied|permission to access|403 Forbidden|로봇이 아닙니다", text[:300]):
                text, page = "", ""                # 차단 페이지는 본문으로 쓰지 않음
            try:                                   # 기사 페이지에 적힌 작성일(구글 날짜가 틀릴 때 대비)
                meta = trafilatura.extract_metadata(page) if page else None
                art["page_date"] = (meta.date if meta and meta.date else "")[:10]
            except Exception:
                art["page_date"] = ""
            if len(text) > 200:
                return text[:2500]
            alt = _html_body(page) if page else ""
            if len(alt) > 200:
                return alt[:2500]
        except Exception:
            pass
    alt = _naver_copy(art.get("title", ""))
    if alt:
        return alt[:2500]
    return (_html_body(page) if page else "")[:2500]   # 마지막으로 og:description(짧아도)


RECENT_SENT: list = []                             # 최근 12시간 동안 보냈거나 모음 대기 중인 기사 제목(AI 중복 판단용)


def summarize(arts: list, _again: bool = False) -> None:
    """각 기사에 summary(요지)와 relevant(관련 여부)를 채운다."""
    for a in ([] if _again else arts):        # 나눠서 다시 처리할 때는 본문을 다시 받지 않음
        a["body"] = fetch_body(a)
        a["relevant"] = True
        text = a["title"] + " " + a["body"]
        if any(w in text for w in EXCLUDE_WORDS):
            a["relevant"] = False                  # 제외 단어
            a["why"] = "제외 단어"
        elif a["body"] and not any(t in text for t in CONTEXT_TERMS):
            a["relevant"] = False                  # 수사·사법 관련 단어가 전혀 없는 기사(동명이인 등)
            a["why"] = "수사·사법 단어 없음"
        # AI 키가 없을 때: 본문 첫머리 120자 정도를 요지로 사용
        first = re.sub(r"\s+", " ", a["body"])[:120]
        a["summary"] = (first.rsplit(" ", 1)[0] + "…") if len(first) == 120 else first
        # 중수청 기사 판별(기본): 제목에 우선 단어, 또는 본문 첫머리 300자에 중수청
        a["priority"] = a["priority"] or any(t in a["body"][:300] for t in LEAD_TERMS)
        a["group"] = None
        a["dup"] = False
        pd_ = a.get("page_date", "")
        # 기사 페이지에서 '월'만 읽고 일을 1일로 채운 날짜(예: 2026-11-01)가 많아 1일은 믿지 않음. 네이버는 시각이 정확해 검사 안 함
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", pd_ or "") and not pd_.endswith("-01") \
                and not a["key"].startswith("nv:") and \
                dt.date.fromisoformat(pd_) < (dt.datetime.now(KST).date() - dt.timedelta(days=2)):
            a["relevant"] = False                  # 기사 페이지상 작성일이 오래된 기사(구글 날짜 오류)
            a["old"] = True
            a["why"] = f"작성일 오래됨({pd_})"
    if not arts:
        return
    if not ANTHROPIC_KEY:
        print("[경고] ANTHROPIC_API_KEY 없음 → 핵심 단어 기준으로만 거름", file=sys.stderr)
        strict_filter(arts)
        return

    blocks = []
    for i, a in enumerate(arts):
        body = a["body"] or "(본문 없음 - 제목만으로 판단)"
        blocks.append(f"<article id=\"{i}\">\n제목: {a['title']}\n본문: {body}\n</article>")

    prompt = (
        "다음은 중대범죄수사청(중수청) 언론동향 모니터링을 위해 수집한 기사들이다.\n"
        f"오늘은 {dt.datetime.now(KST):%Y-%m-%d}이다.\n[기본 사실]\n{FACTS}\n"
        "[요약 주의] 제목의 비유·수사적 표현(예: '한 지붕 아래', '칼끝', '시험대')을 사실관계로 바꾸지 않는다. "
        "기사에 없는 인과관계('~로 ~가 됨', '~에 따라')를 만들지 않는다. 기본 사실과 어긋나게 요약하지 않는다. "
        "본문이 없어 제목만으로 요약할 때는 요약 끝에 '(제목 기준)'을 붙인다. "
        "사진과 짧은 설명뿐인 포토 기사(예: '발언하는 ○○ 청장', '인사 나누는 ○○·○○')는 요약 끝에 '(사진 기사)'를 붙인다.\n"
        "old 판단: 기사 내용이 이미 며칠 지난 사건을 처음 알리는 당시 보도(예: 청장 임명 재가, 출범 첫날 소식 등)여서 "
        "오늘 기준 2일 넘게 지난 기사로 보이면 old=true. 지난 일을 다루더라도 오늘 새로 쓴 분석·후속 기사면 false.\n"
        "각 기사마다 공무원 내부 보고용으로 핵심 요지를 1~2문장, 개조식 '~함/~임' 체로 요약하라. "
        "추측이나 평가는 넣지 말고 기사에 나온 사실만 쓴다.\n"
        "관련성 판단: 다음 중 하나에 해당하면 relevant=true, 아니면 false로 표시하라.\n"
        "① 중수청·공소청(본청·지방청, 청장·간부 포함)의 조직·인사·수사·사건·논란\n"
        "② 수사·기소 분리 등 형사사법체계 개편, 수사권·관할 조정, 보완수사, 형소법 개정의 시행·혼란·평가\n"
        "③ 경찰(국수본)·공수처·특검·특별감찰관 등 유관 수사기관 기사 중 중수청·공소청이나 형사사법체계 개편과 연결된 것\n"
        "④ 위 주제를 다룬 사설·칼럼\n"
        "단어만 우연히 겹친 기사(다른 나라 기관, 일반 사건사고 등)와 동명이인 기사(예: 촬영감독·연예인·운동선수 김지용)는 false.\n"
        "공소청이 개별 형사사건을 송치받거나 기소·구형·상고했다는 단순 사건 보도(선거법 위반 송치, 일반 범죄 기소·재판 등)는 "
        "수사·기소 분리 등 제도 쟁점이나 중수청과 연결되지 않으면 false. 정당 내부·선거 정치 기사, 날씨·생활 기사도 false.\n"
        "단, 정치 기사라도 공소청·검찰 업무와 직결된 쟁점(공소취소, 검찰미래위원회, 조작기소 특검법 등 특검법의 수사·기소 권한, "
        "검찰총장·공소청장 인선)이나 법왜곡죄 고소·고발(판사·검사·수사기관 대상)을 다루면 true.\n"
        "반대로 중수청이 언급된 사설·칼럼·시평, 중수청 관할·이첩·파견 문제를 다룬 기사는 짧게 언급돼도 true.\n"
        "jsc 판단: 기사의 주된 대상이 중수청(중대범죄수사청 본청·지방청, 김지용 청장 등 중수청 간부, 중수청 수사·사건·조직)이면 true. "
        "공소청·경찰·공수처·형사사법체계 일반이 주된 대상이고 중수청은 곁가지로 언급되면 false.\n"
        "group: 같은 사안(같은 사건·발언·발표)을 다룬 기사끼리 같은 숫자를 주고, 다른 사안은 다른 숫자를 준다.\n"
        "dup 판단: 맨 아래 sent 태그 안의 목록은 최근 이미 알림을 보낸 기사 제목이다. 기사가 목록 중 하나와 같은 사안(같은 사건·발표·발언·조치)이고 "
        "새로운 진전(예: 고발→기소처럼 단계가 바뀜, 새 수치·새 당사자 입장 공개)이 없으면 dup=true. "
        "표현만 다른 같은 내용(예: '고발건 배당'과 '수사 착수')은 dup=true. 단, 제목이 [단독]으로 시작하는 기사는 항상 dup=false.\n"
        "출력은 JSON 배열만: [{\"id\": 0, \"summary\": \"...\", \"relevant\": true, \"jsc\": true, \"group\": 1, "
        "\"dup\": false, \"old\": false}, ...]\n\n"
        + "<sent>\n" + ("\n".join("- " + t for t in RECENT_SENT[-80:]) or "(없음)") + "\n</sent>\n\n"
        + "\n\n".join(blocks)
    )
    data, err = None, ""
    for attempt in range(3):                       # 일시 오류(과부하·시간초과·답변 잘림)면 다시 시도
        try:
            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": CLAUDE_MODEL, "max_tokens": 8000,
                      "messages": [{"role": "user", "content": prompt}]},
                timeout=180,
            )
            if r.status_code in (401, 403):        # 키 문제는 다시 해도 안 됨
                err = f"{r.status_code} {r.text[:200]}"
                break
            if r.status_code == 400 and "credit" in r.text.lower():
                err = "크레딧 부족 " + r.text[:200]
                break
            r.raise_for_status()
            res = r.json()
            text = "".join(c.get("text", "") for c in res.get("content", []))
            if res.get("stop_reason") == "max_tokens":
                raise ValueError("답변이 길이 제한에 걸려 잘림")
            data = json.loads(text[text.index("["): text.rindex("]") + 1])
            break
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            try:
                err += " " + r.text[:200]
            except Exception:
                pass
            time.sleep(5 * (attempt + 1))
    if data is None:
        if len(arts) > 4:                          # 실패하면 절반씩 나눠 다시 처리
            print(f"[알림] AI 처리 실패({err[:80]}) → {len(arts)}건을 나눠 다시 시도", file=sys.stderr)
            half = len(arts) // 2
            summarize(arts[:half], _again=True)
            summarize(arts[half:], _again=True)
            return
        print(f"[경고] AI 요약 실패 → 핵심 단어 기준으로만 거름: {err}", file=sys.stderr)
        strict_filter(arts)
        return
    for d in data:
        i = int(d.get("id", -1))
        if 0 <= i < len(arts):
            if d.get("summary"):
                arts[i]["summary"] = d["summary"].strip()
            if arts[i]["relevant"] and not bool(d.get("relevant", True)):
                arts[i]["why"] = "AI 관련 없음"
            arts[i]["relevant"] = arts[i]["relevant"] and bool(d.get("relevant", True))
            arts[i]["dup"] = bool(d.get("dup", False)) and not is_scoop(arts[i]["title"])   # [단독]은 중복 판단 제외
            if d.get("old") and not fresh_date(arts[i]):
                arts[i]["relevant"] = False
                arts[i]["old"] = True
                arts[i]["why"] = "AI 지난 기사"
            if "jsc" in d:
                arts[i]["priority"] = bool(d["jsc"]) or any(t in arts[i]["title"] for t in PRIORITY_TERMS)
            if d.get("group") is not None:
                arts[i]["group"] = str(d["group"])


def fresh_date(a: dict) -> bool:
    """날짜 근거로 '최근 기사'임이 확실한지. AI가 지난 기사(old)로 잘못 보는 일이 잦아(11.13.~26. 스크랩 대조:
    '김병기 고발 사건 중수청 이관', '노재헌 소환 통보' 등 당일 기사가 빠짐) 날짜가 확실하면 AI의 old 판단을 따르지 않음"""
    today = dt.datetime.now(KST).date()
    if a["key"].startswith("nv:") and a.get("pub") and a["pub"].date() >= today - dt.timedelta(days=2):
        return True                                # 네이버는 작성 시각이 정확함
    pd_ = a.get("page_date", "")
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", pd_ or "")) and not pd_.endswith("-01") \
        and dt.date.fromisoformat(pd_) >= today - dt.timedelta(days=2)


def strict_filter(arts: list) -> None:
    """AI 판단을 못 쓸 때: 제목·본문에 핵심 단어가 없는 기사는 제외"""
    for a in arts:
        if a["relevant"] and not any(t in a["title"] + " " + a["body"] for t in CORE_TERMS):
            a["relevant"] = False
            a["why"] = "핵심 단어 없음(AI 실패 시)"


# ── 전송 ─────────────────────────────────────────────────────
def fmt_time(t: dt.datetime, now: dt.datetime) -> str:
    return f"{t:%H:%M}" if t.date() == now.date() else f"{t:%m.%d. %H:%M}"


def group_articles(arts: list) -> list:
    """같은 사안 기사 묶기 → [[대표기사, 관련기사...], ...] (AI group, 없으면 제목 유사도)"""
    groups, by_key = [], {}
    for a in arts:
        key = a.get("group")
        if key is None:
            for g in groups:                       # AI 묶음이 없으면 제목이 비슷한 것끼리
                if similar(g[0]["title"], a["title"]):
                    g.append(a)
                    break
            else:
                groups.append([a])
            continue
        if key in by_key:
            by_key[key].append(a)
        else:
            by_key[key] = [a]
            groups.append(by_key[key])
    return groups


def dedupe(arts: list, seen: dict, state: dict, now: dt.datetime) -> list:
    """같은 기사가 출처 이름·주소만 바뀌어 다시 검색되는 경우 제거"""
    recent = [r[0] for r in state.get("recent", [])] + [p["title"] for p in state.get("pending", [])]
    out, urls = [], set()
    for a in arts:
        th = 0.95 if is_scoop(a["title"]) else 0.75   # [단독]은 사실상 같은 제목(재검색)일 때만 중복 처리
        dup = (a["url"] in urls or ("u:" + a["url"]) in seen
               or any(similar(a["title"], t, th) for t in recent))
        if dup:
            seen[a["key"]] = now.isoformat()
            drop("중복(같은 주소·비슷한 제목)", a["press"], a["title"], now)
            continue
        # 언론사마다 제목을 다르게 단 같은 사건(예: '구본현 구속 기소' 7건) — AI 판단이 안 될 때도 걸러지게
        matches = [] if is_scoop(a["title"]) else [t for t in recent + [x["title"] for x in out] if same_story(a["title"], t)]
        if matches and a.get("priority") and is_followup(a["title"], matches):
            matches = []   # 중수청 기사: 앞 기사들에 없던 진행 단계(영장·구속·기소 등)가 나오면 후속 보도로 보고 보냄
        if matches:
            seen[a["key"]] = now.isoformat()
            drop("중복(같은 사건, 핵심 낱말 겹침)", a["press"], a["title"], now)
            continue
        urls.add(a["url"])
        out.append(a)
    return out


# 제목 핵심 낱말 비교용: 기관명·흔한 수사 용어·날짜 표현은 빼고 비교
_STORY_STOP = set(("중수청 중대범죄수사청 경찰 경찰청 공소청 검찰 공수처 혐의 수사 기소 구속 불구속 송치 출범 이르면 다음주 다음 "
                   "내달 예정 임박 종합 단독 속보 결국 의혹 사건 관련 위해 대한 이번 전국 서울 정부 당국 논란 발표 진행 확인 공개 "
                   "요구 우려 입장 앵커 리포트 자막뉴스 실시간뉴스 뉴스 오늘 어제 지난 올해 내년 대표 의원 장관 위원장 청장 후보자 "
                   "전 현 등 및 첫 끝 만 년 명 건 원 억 조").split())
_JOSA = re.compile(r"(으로|에서|에게|까지|부터|이라|라는|하는|했다|한다|에도|에는|과의|와의|은|는|이|가|을|를|의|에|로|와|과|도|만|서)$")


def story_terms(t: str) -> set:
    t = re.sub(r"\[[^\]]*\]|\([^)]*\)", "", t or "")
    out = set()
    for w in re.findall(r"[가-힣A-Za-z0-9]+", t):
        w2 = _JOSA.sub("", w) if len(w) > 2 else w
        if len(w2) >= 2 and w2 not in _STORY_STOP and not w2.isdigit():
            out.add(w2)
    return out


# 사건 진행 단계 낱말 — 같은 사건이라도 새 단계가 나오면 후속 보도
_STAGES = {
    "압수수색": ("압수수색", "압색"), "소환": ("소환", "출석", "조사받"), "체포": ("체포",),
    "영장": ("영장",), "구속": ("구속",), "기각": ("기각",), "발부": ("발부",), "기소": ("기소",),
    "송치": ("송치",), "선고": ("선고", "판결", "실형", "집행유예", "무죄"), "석방": ("석방", "보석"),
    "착수": ("착수", "입건"), "출범": ("출범", "개청"), "임명": ("임명", "지명", "내정"),
}


def stages(t: str) -> set:
    t = re.sub(r"불구속", "", t or "")
    return {k for k, ws in _STAGES.items() if any(w in t for w in ws)}


def is_followup(title: str, earlier: list) -> bool:
    """앞서 보낸 같은 사건 기사들에 없던 진행 단계가 제목에 나오면 후속 보도"""
    seen_st = set().union(*(stages(t) for t in earlier)) if earlier else set()
    return bool(stages(title) - seen_st)


def same_story(t1: str, t2: str) -> bool:
    """핵심 낱말이 2개 이상, 짧은 쪽 제목의 3분의 1 이상 겹치면 같은 사건"""
    x, y = story_terms(t1), story_terms(t2)
    s = x & y
    return len(s) >= 2 and len(s) / max(1, min(len(x), len(y))) >= 0.34


def similar(t1: str, t2: str, th: float = 0.6) -> bool:
    b = lambda t: {t[i:i + 2] for i in range(len(t) - 1)}
    x, y = b(norm_title(t1)), b(norm_title(t2))
    return bool(x and y) and len(x & y) / min(len(x), len(y)) >= th


def to_record(a: dict, now: dt.datetime) -> dict:
    return {"url": a["url"], "title": a["title"], "press": a["press"], "pub": a["pub"].isoformat(),
            "summary": a["summary"], "group": a.get("group"), "found": now.isoformat(),
            "body": re.sub(r"\s+", " ", a.get("body") or "")[:2500],   # 대본·검증용 원문(요약 AI가 본 것과 같은 길이)
            "jsc": bool(a.get("priority"))}


def from_record(r: dict) -> dict:
    r = dict(r)
    r["pub"] = dt.datetime.fromisoformat(r["pub"])
    return r


def telegraph_page(arts: list, now: dt.datetime, title: str, intro: str) -> str:
    """모음을 텔레그래프(telegra.ph) 웹페이지 한 장으로 → 주소. 텔레그램 안에서 바로 열림(Instant View).
    주소를 아는 사람만 볼 수 있지만 비공개는 아님(공개된 기사 요약만 담음)"""
    api = "https://api.telegra.ph"
    token = (os.getenv("TELEGRAPH_TOKEN") or "").strip()
    if not token:
        r = requests.get(f"{api}/createAccount", params={"short_name": "news-alert", "author_name": "언론보도 모니터링"}, timeout=20)
        token = r.json()["result"]["access_token"]
    nodes = [{"tag": "p", "children": [{"tag": "em", "children": [intro]}]}]
    for n, g in enumerate(group_articles(arts), 1):
        a = g[0]
        nodes.append({"tag": "h4", "children": [f"{n}. {a['title']}"]})
        nodes.append({"tag": "p", "children": [{"tag": "em", "children": [f"{a['press']} · {fmt_time(a['pub'], now)}"]}]})
        if a.get("summary"):
            nodes.append({"tag": "p", "children": [a["summary"]]})
        links = [{"tag": "a", "attrs": {"href": a["url"]}, "children": ["▶ 기사 보기"]}]
        if len(g) > 1:
            links.append(f"  (같은 내용 {len(g) - 1}건: ")
            for j, x in enumerate(g[1:6]):
                links += ([" · "] if j else []) + [{"tag": "a", "attrs": {"href": x["url"]}, "children": [x["press"]]}]
            links.append(")")
        nodes.append({"tag": "p", "children": links})
    content = json.dumps(nodes, ensure_ascii=False)
    if len(content.encode()) > 60000:              # 페이지 용량 제한(64KB) 대비: 요약을 줄임
        for nd in nodes:
            if nd["tag"] == "p" and nd["children"] and isinstance(nd["children"][0], str) and len(nd["children"][0]) > 120:
                nd["children"][0] = nd["children"][0][:120] + "…"
        content = json.dumps(nodes, ensure_ascii=False)
    r = requests.post(f"{api}/createPage", data={"access_token": token, "title": title[:250], "author_name": "언론보도 모니터링",
                                                 "content": content, "return_content": "false"}, timeout=30)
    res = r.json()
    if not res.get("ok"):
        raise RuntimeError(f"텔레그래프 오류: {str(res)[:200]}")
    return res["result"]["url"]


def digest_due(last, now: dt.datetime):
    """모음 보낼 때인지 → (보낼지, 다음 발송 안내). 정해진 시각이 지났고 그 뒤로 아직 안 보냈으면 보냄"""
    if DIGEST_TIMES.lower() == "off":
        due = last is None or now - last >= dt.timedelta(minutes=DIGEST_MINUTES)
        left = 0 if last is None else DIGEST_MINUTES - int((now - last).total_seconds() // 60)
        return due, f"{left}분 뒤"
    slots = []
    for t in DIGEST_TIMES.split(","):
        m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", t)
        if m:
            slots.append(now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0))
    slots.sort()
    for sl in slots:
        if sl <= now and (last is None or last < sl) and now - sl < dt.timedelta(hours=12):
            return True, ""
    later = [sl for sl in slots if sl > now]
    return False, (f"{later[0]:%H:%M}" if later else f"내일 {slots[0]:%H:%M}" if slots else "-")


def format_list(arts: list, now: dt.datetime, header: str) -> list:
    """한 사안 한 줄: 번호. 제목(누르면 기사) — 언론사 · 시각 (같은 내용 n건). 접기 없이 짧게"""
    e = html.escape
    lines = []
    for n, g in enumerate(group_articles(arts), 1):
        a = g[0]
        extra = " · ".join(f"<a href=\"{e(x['url'], quote=True)}\">{e(x['press'])}</a>" for x in g[1:4])
        lines.append(f"{n}. <a href=\"{e(a['url'], quote=True)}\">{e(a['title'])}</a>\n"
                     f"    <i>{e(a['press'])} · {fmt_time(a['pub'], now)}</i>" + (f" · 같은 내용: {extra}" if extra else ""))
    msgs, cur = [], header
    for ln in lines:
        if len(cur) + len(ln) > 3900:
            msgs.append(cur)
            cur = ""
        cur += ln + "\n"
    msgs.append(cur)
    return msgs


def format_messages(arts: list, now: dt.datetime, header: str, collapse: bool = False) -> list:
    e = html.escape
    blocks = []
    for n, g in enumerate(group_articles(arts), 1):
        a = g[0]
        link = f"<a href=\"{e(a['url'], quote=True)}\">기사 보기</a>"
        if len(g) > 1:
            more = ", ".join(f"<a href=\"{e(x['url'], quote=True)}\">{e(x['press'])}</a>" for x in g[1:])
            link += f" · 같은 내용 {len(g) - 1}건: {more}"
        blocks.append(
            f"\n{n}. <b>{e(a['title'])}</b>\n"
            f"   {e(a['press'])} · {fmt_time(a['pub'], now)}\n"
            + (f"   ▸ {e(a['summary'])}\n" if a["summary"] else "")
            + f"   {link}"
        )
    if collapse:                                   # 접힌 상태로(눌러서 펼치기) → 화면을 거의 차지하지 않음
        msgs, cur = [], ""
        for b in blocks:
            if cur and len(cur) + len(b) > 3700:
                msgs.append(cur)
                cur = ""
            cur += b + "\n"
        msgs.append(cur)
        return [(header if i == 0 else "") + f"<blockquote expandable>{m.strip()}</blockquote>" for i, m in enumerate(msgs)]
    msgs, cur = [], header
    for b in blocks:                               # 텔레그램 4096자 제한 대비 분할
        if len(cur) + len(b) > 3900:
            msgs.append(cur)
            cur = ""
        cur += b + "\n"
    msgs.append(cur)
    return msgs


def send_telegram(text: str, silent: bool = False, preview: bool = False) -> None:
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    chat = TG_CHAT.strip()
    payload = {"chat_id": chat, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": not preview, "disable_notification": silent}
    r = requests.post(url, json=payload, timeout=15)
    if r.ok:
        return
    desc = r.json().get("description", r.text) if r.headers.get("content-type", "").startswith("application/json") else r.text
    print(f"[텔레그램 오류] {r.status_code}: {desc}", file=sys.stderr)
    if "parse" in desc.lower():                    # 서식 문제면 서식 없이 다시 보냄
        plain = html.unescape(re.sub(r'<a href="([^"]+)">[^<]*</a>', r"\1", text))
        plain = re.sub(r"</?(?:b|i|blockquote[^>]*)>", "", plain)
        r = requests.post(url, json={"chat_id": chat, "text": plain, "disable_notification": silent,
                                     "disable_web_page_preview": True}, timeout=15)
        if r.ok:
            return
        print(f"[텔레그램 오류] 재시도 실패 {r.status_code}: {r.text}", file=sys.stderr)
    if "chat not found" in desc.lower():
        print("→ TELEGRAM_CHAT_ID가 틀렸거나, 봇에게 먼저 메시지를 보내지 않았습니다.", file=sys.stderr)
    r.raise_for_status()



# ── 음성 브리핑 ───────────────────────────────────────────────
def _kr_holidays():
    try:
        import holidays
        return holidays.KR(years=range(dt.date.today().year - 1, dt.date.today().year + 2))
    except Exception:
        return {}


def audio_due(state: dict, now: dt.datetime):
    """만들 차례면 '아침'/'오후'(시험이면 '시험'), 아니면 None. 평일·비공휴일만, 예정 시각부터 3시간 안"""
    if AUDIO_TEST:
        return "시험"
    if not AUDIO_ON or now.weekday() >= 5 or now.date() in _kr_holidays():
        return None
    last = dt.datetime.fromisoformat(state["last_audio"]) if state.get("last_audio") else None
    for t in AUDIO_TIMES.split(","):
        m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", t)
        if not m:
            continue
        slot = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        if slot + dt.timedelta(minutes=BRIEF_FALLBACK_MIN) <= now < slot + dt.timedelta(hours=3) \
                and (last is None or last < slot):
            return "아침" if slot.hour < 12 else "오후"
    return None


def ask_claude(prompt: str, max_tokens: int = 8000, timeout: int = 150, model: str = "") -> str:
    """음성 브리핑용 AI 호출. 답의 글 부분만 돌려줌(생각 블록 등은 건너뜀). 실패하면 오류 기록 후 ''
    답이 길이 제한에 걸려 잘리면 제한을 늘려 다시 요청함"""
    no_think = True                                # '생각' 단계를 끄고(답이 잘리는 원인) 안 되면 기본으로
    for attempt in range(3):
        try:
            body = {"model": model or CLAUDE_MODEL, "max_tokens": max_tokens,
                    "messages": [{"role": "user", "content": prompt}]}
            if no_think:
                body["thinking"] = {"type": "disabled"}
            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json=body, timeout=timeout,
            )
            if not r.ok:
                if no_think and r.status_code == 400 and "thinking" in r.text:
                    no_think = False
                    continue
                raise RuntimeError(f"{r.status_code} {r.text[:300]}")
            res = r.json()
            text = "".join(c.get("text", "") for c in res.get("content", []) if c.get("type", "text") == "text").strip()
            if res.get("stop_reason") == "max_tokens":
                AUDIO_ERR.append(f"답변 잘림(max_tokens={max_tokens}, 글 {len(text)}자, 블록={[c.get('type') for c in res.get('content', [])]})")
                max_tokens = min(max_tokens * 2, 32000)
                continue
            if not text:
                raise RuntimeError(f"빈 답변 stop={res.get('stop_reason')} 블록={[c.get('type') for c in res.get('content', [])]}")
            return text
        except Exception as e:
            msg = f"{type(e).__name__}: {str(e)[:300]}"
            AUDIO_ERR.append(msg)
            print(f"[음성 AI 오류] {msg}", file=sys.stderr)
            time.sleep(5)
    return ""


_STOP = {"중수청", "공소청", "중대범죄수사청", "단독", "속보", "종합", "기자", "뉴스", "관련", "의혹", "사건", "논란"}


def _words(t: str) -> set:
    return {w for w in re.findall(r"[가-힣A-Za-z0-9]{2,}", re.sub(r"\[[^\]]*\]", " ", t)) if w not in _STOP}


def audio_cluster(groups: list) -> list:
    """여러 실행에 걸쳐 들어온 같은 사안 기사들을 하나로 묶음 → [[그룹번호...], ...] (중요한 사안부터)
    AI로 묶고, 실패하면 제목·요약에 겹치는 단어가 많은 것끼리 묶음"""
    n = len(groups)
    if ANTHROPIC_KEY and n > 1:
        lines = "\n".join(f"[{i}] {g[0]['title']} / {(g[0]['summary'] or '')[:120]}" for i, g in enumerate(groups))
        prompt = ("아래 기사 목록에서 같은 사안(같은 사건·발표·인사·조치)을 다룬 기사 번호끼리 묶어라. "
                  "표현이 달라도 같은 일이면 같은 묶음이다. 같은 쟁점을 다룬 후속·반응·해설 기사도 같은 묶음이다"
                  "(예: '공소청-중수청, 국제회의 두고 또 충돌'과 '국제회의 개최 권한 놓고 중수청·공소청 충돌'은 같은 사안). "
                  "같은 기관·같은 주제라도 다른 일(예: 장비 이관과 청사 이전)은 따로 둔다. "
                  "묶음 순서는 중요도 순(기사 수가 많을수록, 중수청 조직·청장·주요 사건일수록 앞). "
                  "모든 번호가 정확히 한 번씩 들어가야 한다. JSON 배열만 출력: [[0,3],[1],[2,4]]\n\n" + lines)
        try:
            text = ask_claude(prompt, 4000, model=VERIFY_MODEL)   # 묶기는 판단이 중요해 검증과 같은 모델 사용
            data, _ = json.JSONDecoder().raw_decode(text[text.index("["):])   # 뒤에 설명이 붙어도 첫 배열만
            out, used = [], set()
            for c in data:
                c = [int(i) for i in (c if isinstance(c, list) else [c]) if 0 <= int(i) < n and int(i) not in used]
                used.update(c)
                if c:
                    out.append(c)
            out += [[i] for i in range(n) if i not in used]
            return out
        except Exception as e:
            AUDIO_ERR.append(f"사안 묶기: {type(e).__name__}: {str(e)[:200]}")
            print(f"[음성 사안 묶기 AI 오류] {type(e).__name__}: {e}", file=sys.stderr)
    ws = [_words(g[0]["title"] + " " + (g[0]["summary"] or "")[:80]) for g in groups]
    out, where = [], {}
    for i in range(n):
        for j in range(i):
            inter = ws[i] & ws[j]
            jac = len(inter) / max(1, len(ws[i] | ws[j]))
            if jac >= 0.3 or (len(inter) >= 3 and jac >= 0.15):
                out[where[j]].append(i)
                where[i] = where[j]
                break
        else:
            where[i] = len(out)
            out.append([i])
    out.sort(key=lambda c: -sum(len(groups[i]) for i in c))
    return out


def closing_text(label: str, now: dt.datetime, more: int) -> str:
    """맺음말(AI가 쓰지 않고 프로그램이 고정): 자세한 내용 안내 + 인사. 기관 응원 멘트는 넣지 않음(중립성)"""
    where = "함께 보내 드린 카드뉴스와 텔레그램 알림의 기사 링크에서" if CARD_ON else "텔레그램 알림의 기사 링크에서"
    pm = label == "오후" or (label not in ("아침", "오후") and now.hour >= 12)
    greet = ("편안한 주말 보내십시오." if now.weekday() == 4 else "오늘 하루도 수고 많으셨습니다.") if pm else "오늘 하루도 힘내십시오."
    return ((f"이 밖에도 중수청 관련 소식이 {more}가지 더 있습니다. " if more else "")
            + f"자세한 내용은 {where} 확인하실 수 있습니다. 지금까지 중수청 언론보도 브리핑이었습니다. " + greet)


def intro_text(label: str, now: dt.datetime) -> str:
    return f"{now.month}월 {now.day}일 {'월화수목금토일'[now.weekday()]}요일, 중수청 언론보도 {label} 브리핑입니다."


# ══ 새 방식 브리핑(11.27. 재설계): AI는 사실을 '뽑기'만, 원고는 확인된 사실로만 ══════════════════
# 1) 사실 뽑기: AI가 본문에서 사실마다 근거 문장을 그대로 옮겨 붙이고, 프로그램이 그 문장이 본문에 실제로 있는지 글자로 확인
# 2) 원고 쓰기: AI는 확인된 사실 목록만 보고 쓰며 문장마다 사실 번호를 담. 숫자·기관명·날짜가 그 사실에 없으면 문장을 뺌
# 3) 본문 없는 사안(제목뿐)은 원고에서 빼고 목록으로만 알림
WRITE_MODEL = (os.getenv("WRITE_MODEL") or "").strip() or VERIFY_MODEL      # 사실 뽑기·원고 쓰기 모델(기본 Sonnet)
BRIEF_MODE = (os.getenv("BRIEF_MODE") or "old").strip().lower()             # old | new | both(기존 발송 + 새 방식 원고 비교 발송)
V2_LOG: list = []                                                          # 새 방식에서 버린 사실·문장(점검 기록)


def _squash(t: str) -> str:
    return re.sub(r"[\s\"'“”‘’`·…]+", "", _num_norm(html.unescape(t or "")))


def _json_obj(text: str):
    """답에서 JSON 객체를 찾아 읽음(앞뒤 설명·코드 블록 표시·'[사안 1]' 같은 대괄호가 있어도). 못 찾으면 오류 + 답 앞부분 기록"""
    dec = json.JSONDecoder()
    for m in re.finditer(r"\{", text or ""):
        try:
            obj = dec.raw_decode(text[m.start():])[0]
            if isinstance(obj, dict) and obj:
                return obj
        except ValueError:
            continue
    V2_LOG.append(f"AI 답을 읽지 못함(앞부분: {(text or '(빈 답)')[:120]!r})")
    raise ValueError(f"JSON 없음: {(text or '(빈 답)')[:120]!r}")


def extract_facts(groups: list, now: dt.datetime) -> tuple:
    """→ ({사안번호: [{id, text, quote}]}, [제목뿐인 사안 번호])"""
    blocks, title_only, bodies = [], [], {}
    for n, g in enumerate(groups, 1):
        arts = [(i, x) for i, x in enumerate(g[:4]) if (x.get("body") or "").strip()]
        if not arts:
            title_only.append(n)
            continue
        bodies[n] = " ".join(x["title"] + " " + x["body"][:2500] for _, x in arts)
        pub = arts[0][1].get("pub")
        try:
            pub = pub if isinstance(pub, dt.datetime) else dt.datetime.fromisoformat(str(pub))
            when = f"(작성 {pub.month}월 {pub.day}일)"
        except Exception:
            when = ""
        blocks.append(f"[사안 {n}] {when}\n" + "\n".join(f"<기사> 제목: {x['title']}\n본문: {x['body'][:2500]}" for _, x in arts))
    if not blocks:
        return {}, title_only
    head = (
        "아래 기사 본문에서 핵심 사실을 중요한 순서로 최대 5개 뽑아라. 글을 쓰는 것이 아니라 사실을 옮겨 적는 일이다.\n"
        "[형식] JSON만 출력: {\"N\": [{\"id\": \"Na\", \"text\": \"...\", \"quote\": \"...\"}]} (N은 사안 번호, id는 Na·Nb…)\n"
        "- text: 사실 하나를 짧은 평서문으로. 숫자·기관명·직함·이름은 본문 표기 그대로(단위·이름을 바꾸거나 늘리지 않음).\n"
        "- quote: 그 사실의 근거가 된 본문 문장을 한 글자도 바꾸지 않고 그대로 옮긴다(30~150자). 프로그램이 본문과 글자 대조한다.\n"
        "- 기자의 해설·평가·전망('~할 전망', '과제로 떠올랐다', '우려가 나온다')은 사실로 뽑지 않는다. "
        "누가 한 말이면 '○○는 ~라고 말함'처럼 말한 사람을 밝혀 뽑는다.\n"
        "- 기사 속 '오늘·어제·지난달' 같은 말은 기사 작성일 기준이다. text에는 날짜로 바꿔 적는다(작성일은 사안 머리에 있음).\n"
        "- 제목의 비유·수사적 표현을 사실로 바꾸지 않는다. 본문에 없는 인과관계를 만들지 않는다.\n"
        f"- 오늘은 {now:%Y-%m-%d}. 기본 사실(본문이 이와 다르게 읽히면 그 사실은 뽑지 않는다):\n{FACTS}\n\n")
    # 사안마다 따로 물음(한 번에 물으면 답이 길어 잘리고 시간이 초과됨 — 11.27. 시험) → 동시에 4개씩
    from concurrent.futures import ThreadPoolExecutor

    def one(block):
        try:
            return _json_obj(ask_claude(head + block, 8000, timeout=240, model=WRITE_MODEL))
        except Exception as e:
            V2_LOG.append(f"사실 뽑기 실패: {block[:12]} {type(e).__name__}")
            return {}
    data = {}
    with ThreadPoolExecutor(max_workers=4) as ex:
        for d in ex.map(one, blocks):
            if isinstance(d, dict):
                data.update(d)
    out = {}
    for k, lst in (data.items() if isinstance(data, dict) else []):
        try:
            n = int(re.sub(r"\D", "", str(k)))
        except ValueError:
            continue
        if n not in bodies:
            continue
        src = _squash(bodies[n])
        for f in lst if isinstance(lst, list) else []:
            try:
                fid, text, quote = str(f["id"]), str(f["text"]).strip(), str(f["quote"]).strip()
            except Exception:
                continue
            if len(_squash(quote)) < 12 or _squash(quote) not in src:
                V2_LOG.append(f"사실 버림(근거 문장이 본문에 없음) {n}: {text[:40]}")
                continue
            bad = unsupported(_fix_facts(text), quote + " " + bodies[n][:300], now)
            if bad:
                V2_LOG.append(f"사실 버림(근거에 없는 {', '.join(bad)}) {n}: {text[:40]}")
                continue
            out.setdefault(n, []).append({"id": fid, "text": _fix_facts(text), "quote": quote})
    for n in bodies:
        if n not in out:
            title_only.append(n)                    # 확인된 사실이 하나도 없으면 제목뿐인 사안과 같이 취급
    return out, sorted(title_only)


def _check_sent(t: str, cited: list, now: dt.datetime) -> list:
    """문장 속 숫자·기관명이 인용한 사실(근거 문장 포함)에 있는지 → 없는 것 목록"""
    ref = " ".join(f["text"] + " " + f["quote"] for f in cited)
    bad = unsupported(t, ref, now)
    if re.search(r"(?<![가-힣])(오늘|어제|내일|그제|이번 주|지난주)(?![가-힣])", t):
        bad.append("상대 날짜")
    return bad


def compose_v2(facts: dict, groups: list, label: str, now: dt.datetime, more: int) -> tuple:
    """확인된 사실만으로 원고(기존 형식: 첫머리, [사안 n] 문단, 맺음말)와 카드 → (원고, 카드, 사안 번호 목록)"""
    order = [n for n in sorted(facts) if facts[n]]
    if not order:
        return "", [], []
    flist = "\n\n".join(f"[사안 {n}]\n" + "\n".join(f"{f['id']}: {f['text']}" for f in facts[n]) for n in order)
    prompt = (
        "너는 중대범죄수사청 직원이 출근길에 귀로 듣는 라디오 뉴스 앵커다. 아래 [확인된 사실]만으로 원고와 카드뉴스 문구를 써라.\n"
        "[절대 규칙] 확인된 사실에 없는 내용(숫자·날짜·기관명·직함·이름·인과관계·평가·전망)은 한 글자도 넣지 않는다. "
        "문장마다 근거가 된 사실 id를 f에 적는다. 프로그램이 문장 속 숫자·기관명을 그 사실과 대조해 없으면 문장을 지운다.\n"
        "[형식] JSON만 출력:\n"
        "{\"paras\": [{\"n\": 1, \"sents\": [{\"t\": \"문장\", \"f\": [\"1a\"]}]}],\n"
        " \"cards\": [{\"n\": 1, \"tag\": \"수사\", \"headline\": {\"t\": \"28자 이내 명사형 제목\", \"f\": [\"1a\"]}, "
        "\"points\": [{\"t\": \"40자 이내 개조식\", \"f\": [\"1b\"]}]}]}\n"
        f"- 사안마다 문단 하나, 카드 하나. 카드 tag는 {', '.join(CARD_TAGS)} 중 하나. 카드 요점은 사실이 있으면 3개.\n"
        "[원고 말투]\n"
        "- 모든 문장은 '~습니다/~입니다'. 한 문장 40자 안팎, 정보 하나. 첫 문장은 주어(누가·어느 기관이)를 밝히고 무슨 일인지 말한다.\n"
        "- 첫머리·맺음말·넘김말('먼저', '이어서', '다음 소식입니다')은 쓰지 않는다(프로그램이 붙임).\n"
        "- 숫자는 사실에 적힌 표기 그대로. 한 문장에 숫자 둘을 '~인'으로 잇지 않는다(금액 먼저, 비율은 다음 문장).\n"
        "- '오늘·어제' 대신 날짜를 쓴다. 인과 연결어('그래서', '이에 따라', '이 때문에')는 사실에 그 관계가 적혀 있을 때만.\n"
        "- 해석·평가·전망 표현('셈입니다', '떠올랐습니다', '전망입니다', '그쳤습니다', '드문', '이례적')을 쓰지 않는다. "
        "누가 한 말은 '○○는 ~라고 말했습니다'로.\n"
        "- 언론사 이름, 괄호, 따옴표, 기호, 영문 약어, 한자 약칭을 쓰지 않는다. 같은 내용을 다른 사안에서 되풀이하지 않는다.\n"
        f"- 오늘은 {now:%Y-%m-%d}. 기본 사실:\n{FACTS}\n\n[확인된 사실]\n{flist}")
    data = _json_obj(ask_claude(prompt, 12000, timeout=300, model=WRITE_MODEL))
    by_id = {f["id"]: (n, f) for n in order for f in facts[n]}

    def ok(item, sec_n, where):
        t = _fix_facts(str(item.get("t", "")).strip())
        ids = [str(x) for x in item.get("f", []) if str(x) in by_id]
        cited = [by_id[i][1] for i in ids if by_id[i][0] == sec_n]
        if not t:
            return ""
        if not cited:
            V2_LOG.append(f"{where} 버림(근거 사실 없음): {t[:40]}")
            return ""
        bad = _check_sent(t, cited, now)
        if bad:
            V2_LOG.append(f"{where} 버림(사실에 없는 {', '.join(bad)}): {t[:40]}")
            return ""
        return t

    paras = []
    for p_ in data.get("paras", []) if isinstance(data, dict) else []:
        try:
            i = int(p_["n"])
        except Exception:
            continue
        if i not in order:
            continue
        sents = [ok(x, i, f"원고 사안 {i}") for x in p_.get("sents", []) if isinstance(x, dict)]
        sents = [x if x.endswith((".", "?", "!")) else x + "." for x in sents if x]
        if sents:
            paras.append((order.index(i) + 1, " ".join(sents)))
    cards = []
    for c in data.get("cards", []) if isinstance(data, dict) else []:
        try:
            i = int(c["n"])
        except Exception:
            continue
        if i not in order:
            continue
        pts = [ok(x, i, f"카드 사안 {i}") for x in c.get("points", []) if isinstance(x, dict)]
        pts = [x for x in pts if x][:3]
        head = ok(c.get("headline") or {}, i, f"카드 사안 {i} 제목") or (pts[0] if pts else "")
        if head and pts:
            cards.append((order.index(i) + 1, {"tag": c.get("tag") if c.get("tag") in CARD_TAGS else "기타", "headline": head, "points": pts}))
    lab = "오후" if label == "시험" and now.hour >= 12 else ("아침" if label == "시험" else label)
    raw = "\n\n".join([intro_text(lab, now)] + [f"[사안 {i}] {t}" for i, t in sorted(paras)] + [closing_text(lab, now, more)])
    card_by = dict(cards)
    return raw, [card_by.get(i) or _card_fallback(groups[order[i - 1] - 1]) for i in range(1, len(order) + 1)], order


def brief_v2(use: list, label: str, now: dt.datetime, more: int) -> dict:
    """새 방식 전체 실행 → {script, raw, cards, groups, removed, verified, title_only, n_facts, log}"""
    V2_LOG.clear()
    facts, title_only = extract_facts(use, now)
    raw, cards, order = compose_v2(facts, use, label, now, more)
    groups = [use[n - 1] for n in order]
    lab = "오후" if label == "시험" and now.hour >= 12 else ("아침" if label == "시험" else label)
    if not raw:
        return {"script": "", "title_only": title_only, "log": list(V2_LOG), "n_facts": 0}
    raw2, cards2, removed, verified = verify_briefing(raw, cards, groups, now, closing_text(lab, now, more), intro_text(lab, now))
    return {"script": speakable(raw2), "raw": raw2, "cards": cards2, "groups": groups, "removed": removed,
            "verified": verified, "title_only": title_only, "n_facts": sum(len(v) for v in facts.values()),
            "log": list(V2_LOG)}


def send_v2_compare(v2: dict, use: list, label: str, reply_to: int) -> None:
    """나란히 운영: 새 방식 원고를 글로(무음) 보내 기존 브리핑과 비교하게 함"""
    e = html.escape
    paras = [p.strip() for p in re.split(r"\n\s*\n", v2.get("script") or "") if p.strip()]
    txt = (f"🧪 <b>새 방식 원고 (비교용 · {label})</b>\n확인된 사실 {v2.get('n_facts', 0)}개로만 작성 · "
           f"사실·문장 버림 {len(v2.get('log', []))}건 · 원문 검증에서 뺌 {len(v2.get('removed') or [])}건\n\n"
           + ("\n\n".join(f"{k}) {e(p)}" for k, p in enumerate(paras, 1)) or "(확인된 사실이 없어 원고를 만들지 않았습니다)"))
    if v2.get("title_only"):
        txt += "\n\n📎 <b>본문을 못 읽어 원고에서 뺀 사안</b>\n" + "\n".join(
            f"· {e(use[n - 1][0]['title'][:60])} ({e(use[n - 1][0]['press'])})" for n in v2["title_only"])
    if v2.get("log"):
        txt += "\n\n<blockquote expandable>버린 사실·문장\n" + "\n".join(e(x) for x in v2["log"][:30]) + "</blockquote>"
    for i in range(0, len(txt), 3900):
        payload = {"chat_id": TG_CHAT.strip(), "text": txt[i:i + 3900], "parse_mode": "HTML",
                   "disable_web_page_preview": True, "disable_notification": True}
        if reply_to:
            payload["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", json=payload, timeout=30)
        if not r.ok:                               # 잘린 HTML 태그 등으로 실패하면 서식 없이 다시
            payload.pop("parse_mode")
            payload["text"] = re.sub(r"<[^>]+>", "", payload["text"])
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", json=payload, timeout=30)


def audio_script(groups: list, label: str, since: dt.datetime, now: dt.datetime, more: int) -> str:
    """AI가 뉴스 앵커 원고로 정리. 실패하면 요약을 그대로 이어 붙임"""
    items = []
    for n, g in enumerate(groups, 1):
        sums = []
        for x in g[:4]:                            # 같은 사안 여러 기사의 요약을 합쳐 한 번에 전하게
            if x["summary"] and x["summary"] not in sums:
                sums.append(x["summary"])
        pub = g[0].get("pub")
        try:
            pub = pub if isinstance(pub, dt.datetime) else dt.datetime.fromisoformat(str(pub))
            when = f" (작성 {pub.month}월 {pub.day}일)"
        except Exception:
            when = ""
        items.append(f"[사안 {n}] 제목: {g[0]['title']}{when}\n관련 기사 {len(g)}건 요약:\n" + "\n".join(sums or ["(요약 없음)"])
                     + "\n" + _excerpts(g))
    intro = intro_text(label, now)
    outro = closing_text(label, now, more)
    if ANTHROPIC_KEY:
        prompt = (
            "너는 아침·저녁 라디오 뉴스 앵커다. 아래 기사들로 3분 안팎의 라디오 뉴스 원고를 써라. "
            "듣는 사람은 출근길이나 퇴근길에 귀로만 듣는 중대범죄수사청(중수청) 직원이다.\n"
            f"- 첫머리는 그대로: {intro}\n- 맺음말은 그대로: {outro}\n"
            f"[배경] 오늘은 {now:%Y-%m-%d}이다. 기본 사실:\n{FACTS}\n"
            "요약과 기사 본문이 다르면 본문을 따른다. 본문에 없는 내용은 쓰지 않는다. '(제목 기준)' 요약은 제목에 적힌 사실만 전한다. "
            "요약의 '신설 중수청'은 '새로 출범한 중수청'으로 쓰고, '새로 생기는', '신설될', '출범할' 같은 미래 표현은 쓰지 않는다.\n"
            "[구성]\n"
            "- '(제목 기준)' 요약만 있는 사안은 '~라는 보도가 나왔습니다' 한 문장으로 전하고, '~라는 내용입니다', '~로 보도됐습니다'처럼 쓰지 않는다. "
            "제목에 없는 평가('드문 발언' 등)를 붙이지 않는다.\n"
            "- 원고는 첫머리 문단으로 시작하고, 사안 문단들, 맺음말 문단 순서로 끝낸다. 첫머리를 뒤에 다시 쓰지 않는다.\n"
            "- 기사 속 '오늘·어제·내일·이번 주' 같은 말은 기사 작성일 기준이다. 원고에서는 '25일'처럼 날짜로 바꿔 쓰고, "
            f"'오늘'은 그 일이 정말 오늘({now.month}월 {now.day}일) 일어난 경우에만 쓴다.\n"
            "- 한 사안의 내용을 다른 사안 문단에서 미리 말하거나 되풀이하지 않는다.\n"
            "- 사안 번호 순서대로, 사안 하나당 한 문단으로 전한다(같은 사안 안의 반응·발언은 그 문단 안에서). "
            "다만 한 사안 묶음 안에 서로 다른 일이 섞여 있으면 억지로 이어 붙이지 말고 별도 문단으로 나눈다. "
            "'첫 번째, 두 번째' 같은 순서 숫자는 쓰지 않는다. "
            "'사설 소식', '단독 소식'처럼 기사 형식을 소식 이름으로 쓰지 않는다. 사설·칼럼은 '한 신문은 사설에서 ~라고 지적했습니다'처럼 전한다.\n"
            "- 한 사안은 딱 한 번만 다룬다. 같은 사안의 여러 요약은 합쳐서 겹치는 내용 없이 전한다. "
            "앞에서 이미 말한 사실을 뒤 사안에서 되풀이하지 않는다.\n"
            "- 사안마다 핵심을 한 문장으로 먼저 말하고(무슨 일인지), 이어서 배경이나 의미를 1~2문장 덧붙인다.\n"
            "- 사안 문단은 반드시 '[사안 n]'(n은 아래 사안 번호)으로 시작한다. 한 사안을 두 문단으로 나누면 두 문단 모두 같은 번호를 붙인다. "
            "'먼저', '이어서', '다음 소식입니다', '마지막 소식입니다' 같은 넘김말은 쓰지 않는다(프로그램이 붙인다). "
            "각 사안 문단의 첫 문장은 앞 사안을 몰라도 이해되게 주어(누가·어느 기관이)를 밝혀 쓴다.\n"
            "- 언론사 이름이나 '보도에 따르면' 같은 출처 표현은 말하지 않는다. 사실 자체를 바로 전한다.\n"
            "[말투]\n"
            "- 모든 문장은 반드시 '~습니다', '~입니다'로 끝낸다(방송 뉴스 말투). 요약에 있는 '~함, ~임, ~됨, ~했음, ~예정' 같은 개조식 끝맺음을 그대로 옮기지 않는다. "
            "예: '회의에 불참함' → '회의에 불참했습니다', '조직이 개편되었음' → '조직이 개편되었습니다', '첫 사례임' → '첫 사례입니다', '발표할 예정' → '발표할 예정입니다'.\n"
            "- 한 문장은 40자 안팎으로 짧게 끊는다. 한 문장에 정보는 하나만.\n"
            "- 신문 제목식 표현(명사로 끝나는 문장, 생략된 조사, '…' 줄임)을 쓰지 말고 완전한 문장으로 풀어 말한다.\n"
            "- 사람 직함은 요약에 나온 그대로만 쓴다. 요약에 없는 직함·소속을 붙이지 않는다(기억에 의존한 직함은 틀릴 수 있음. "
            "예: 요약에 '한동훈 딸'이라고만 있으면 '한동훈 씨의 딸'로 쓰고 장관·대표 같은 직함을 붙이지 않는다). 어려운 법률 용어는 쉬운 말로 한 번 풀어 준다(예: 불송치, 즉 경찰이 사건을 검찰로 넘기지 않기로 한 것).\n"
            "- 숫자는 듣기 쉽게: '87건', '3억 원', '11월 16일'. 퍼센트는 '퍼센트'로.\n"
            "- 한 문장에 숫자를 여러 개 겹쳐 '약 76퍼센트인 4억 원은'처럼 '~인'으로 잇지 않는다. 금액·건수를 먼저 말하고 비율은 "
            "'전체의 약 76퍼센트입니다'처럼 따로 말한다.\n"
            "- 기관명은 요약·본문에 적힌 그대로 쓴다. 줄여 쓴 이름을 정식 명칭으로 늘리거나('서울남부' → '서울남부지방공소청'), "
            "부서 이름을 기관 이름으로 바꾸지 않는다('서울동부 중대범죄기소제2부'를 '서울동부공소청'으로 쓰지 않음).\n"
            "- 괄호, 따옴표, 대괄호, 기호(·, →, ~, /), 영문 약어는 쓰지 않는다. [단독] 표시는 읽지 않는다.\n"
            "[금지]\n"
            "- 제목·요약에 없는 사실, 숫자, 평가, 전망을 지어내지 않는다. 앵커의 개인 의견을 넣지 않는다. "
            "'~한 셈입니다', '도마에 올랐습니다', '과제로 떠올랐습니다', '부상했습니다', '그쳤습니다', '~할 전망입니다', '중요해질 것입니다'처럼 "
            "해석·평가·전망을 덧붙이는 표현도 쓰지 않는다(기사에 나온 전망은 '○○는 ~할 것으로 내다봤습니다'처럼 누구의 말인지 밝힌다). "
            "'후'와 '이후', '처음'과 '첫'처럼 같은 뜻의 말을 한 문장에 겹쳐 쓰지 않는다. "
            "요약이 '과제로 제기됨'이면 '과제로 제기됐습니다', 수치는 '~였습니다'처럼 있는 그대로 전한다.\n"
            "- 문장과 문장 사이의 인과·시간 관계를 지어내지 않는다. '이에 따라', '이 때문에', '같은 날', '이후'는 요약에 그 관계가 적혀 있을 때만 쓴다. "
            "날짜가 요약에 있으면 '같은 날' 대신 '23일'처럼 날짜를 그대로 쓴다. "
            "인사 지명은 소속 관계를 바꾸지 않는다(중수청과 경찰청은 원래 행정안전부 소속이다): '지명에 따라 ~ 산하가 됐다'처럼 쓰지 않는다.\n"
            "- 앞 문단에서 이미 설명한 용어나 사실(예: 시스템 이름 설명)은 뒤에서 다시 설명하지 않고 이름만 쓴다.\n"
            "- 한자 약칭(李, 尹, 故, 與, 野, 檢)은 한글로 풀어 쓴다(이재명 대통령, 고, 여당, 야당, 검찰). 영문 약어는 한글 소리로 쓴다(NDFAS는 엔디파스).\n"
            "- 첫머리, 각 사안, 맺음말은 각각 한 문단으로 쓰고 문단 사이는 빈 줄로 구분한다(읽을 때 쉬는 자리).\n"
            "- 제목, 번호, 소제목, 마크다운 기호 없이 소리 내어 읽을 문장만 출력한다. 전체 1,800자 이내.\n\n"
            + "\n\n".join(items)
        )
        text = ask_claude(prompt)
        src = "\n".join(_src_text(g) for g in groups)   # 대본 AI도 본문 발췌를 받으므로 본문까지 대조(본문에 있는 기관명은 정상)
        bad = sorted({o for o in _ORG_RE.findall(text) if o not in src})
        if len(text) > 50 and bad:                 # 요약에 없는 기관명(예: 서울중수청 → 서울중앙지검)이면 한 번 고쳐 쓰게
            AUDIO_ERR.append(f"대본 기관명 불일치: {', '.join(bad)}")
            again = ask_claude(prompt + "\n\n[고쳐 쓰기] 앞서 쓴 원고에 요약에 없는 기관명이 있었다: "
                               + ", ".join(bad) + ". 기관명은 요약에 적힌 그대로 써라.")
            if len(again) > 50 and len({o for o in _ORG_RE.findall(again) if o not in src}) < len(bad):
                text = again
        if len(text) > 50:
            if "브리핑이었습니다" not in text[-60:]:      # 끝이 잘렸으면 마지막 완성 문장까지만 + 맺음말
                AUDIO_ERR.append(f"대본 끝 누락 보정(원래 {len(text)}자)")
                cut = max(text.rfind("니다."), text.rfind("니다\n"))
                text = (text[:cut + 3] if cut > 0 else text) + "\n\n" + outro
            return text
    parts = [intro]
    for n, g in enumerate(groups, 1):
        a = g[0]
        parts.append(f"[사안 {n}] " + tidy_summary(a["summary"] or a["title"]))
    parts.append(outro)
    return "\n\n".join(parts)


_POLITE = [  # 문장 끝 개조식·해라체 → 합니다체 (AI가 놓쳐도 음성은 존댓말로)
    (r"(였|었|았|했|됐|겠)(음|다)", r"\1습니다"), (r"되었(음|다)", "되었습니다"),
    (r"(있|없|같|않)(음|다)", r"\1습니다"), (r"(하|되)?였음", r"\1였습니다"),
    (r"함", "했습니다"), (r"됨", "됐습니다"), (r"임", "입니다"), (r"이다", "입니다"),
    (r"한다", "합니다"), (r"된다", "됩니다"),
    (r"(예정|전망|방침|계획|입장|상황|상태|방안|취지|계획| 중)", r"\1입니다"),
]


_NOUN_M = ("처음", "다음", "지금", "이름", "사람", "마음", "느낌", "요금", "기금", "소금", "바람", "고름", "기름", "모임", "구름", "얼음", "걸음")
_PAST = {"ㅣ": "ㅕ", "ㅗ": "ㅘ", "ㅜ": "ㅝ", "ㅡ": "ㅓ", "ㅏ": "ㅏ", "ㅓ": "ㅓ", "ㅐ": "ㅐ", "ㅕ": "ㅕ", "ㅔ": "ㅔ", "ㅚ": "ㅙ"}
_V = "ㅏㅐㅑㅒㅓㅔㅕㅖㅗㅘㅙㅚㅛㅜㅝㅞㅟㅠㅡㅢㅣ"


def _nominal_past(body: str) -> str:
    """'밝힘'→'밝혔습니다', '나옴'→'나왔습니다', '전해짐'→'전해졌습니다' (받침 ㅁ 명사형 어미)"""
    if len(body) < 2 or not ("가" <= body[-1] <= "힣") or body.endswith(_NOUN_M) or body.endswith(("함", "됨", "임", "음")):
        return body
    code = ord(body[-1]) - 0xAC00
    if code % 28 != 16:                            # 받침 ㅁ이 아니면 그대로
        return body
    cho, jung = code // 588, (code % 588) // 28
    v = _PAST.get(_V[jung])
    if not v:
        return body
    return body[:-1] + chr(0xAC00 + cho * 588 + _V.index(v) * 28 + 20) + "습니다"   # 받침 ㅆ


def polite(text: str) -> str:
    out = []
    for sent in re.split(r"(?<=[.!?\n])", text):
        m = re.match(r"(.*?)([.!?]?)(\s*)$", sent, re.S)
        body, punct, sp = m.group(1), m.group(2), m.group(3)
        if body and not re.search(r"(니다|니까|세요|시오|까요|어요|에요|예요|해요|죠)$", body):
            last2 = body[-2:]
            if len(last2) == 2 and last2[1] in "다음" and "가" <= last2[0] <= "힣" and (ord(last2[0]) - 0xAC00) % 28 == 20:
                body = body[:-1] + "습니다"            # 밝혔다·밝혔음·보였다 등 받침 ㅆ + 다/음
            body = _nominal_past(body)
            for pat, sub in ([] if body.endswith("니다") else _POLITE):
                new = re.sub(pat + r"$", sub, body)
                if new != body:
                    body = new
                    break
            if body and punct == "" and re.search(r"[가-힣]$", body):
                punct = "."
        out.append(body + punct + sp)
    return "".join(out)


_PRON = [  # (정규식, 바꿀 말) — 귀로 들을 때 어색한 표기를 소리 나는 대로
    (r"([A-Za-z]{2,})\s?\(([가-힣\s]{1,20})\)", r"\2"),           # NDFAS(엔디파스) → 엔디파스
    (r"([가-힣]+)\s?\(([A-Za-z0-9\s\-]{1,20})\)", r"\1"),          # 엔디파스(NDFAS) → 엔디파스
    (r"\(([\u4e00-\u9fff]{1,4})\)", ""),                           # 고(故) → 고
    (r"李\s*대통령", "이재명 대통령"), (r"李", "이재명 대통령"), (r"尹\s*전\s*대통령", "윤석열 전 대통령"),
    (r"尹", "윤석열 전 대통령"), (r"故\s*", "고 "), (r"與", "여당"), (r"野", "야당"), (r"檢", "검찰"),
    (r"警", "경찰"), (r"靑", "청와대"), (r"軍", "군"), (r"美", "미국"), (r"中(?=[\s,])", "중국"), (r"日(?=[\s,])", "일본"),
    (r"(\d{1,2})\.\s?(\d{1,2})\.(?!\d)", r"\1월 \2일"),          # 11.17. → 11월 17일
    (r"(\d{1,2}):(\d{2})", r"\1시 \2분"),                         # 9:20 → 9시 20분
    (r"(\d)\s?%", r"\1퍼센트"), (r"(\d)\s?~\s?(\d)", r"\1에서 \2"),
    (r"\([^)]{0,40}\)", " "), (r"(^|[\s.])[-–•]\s+", r"\1 "), (r"[/|]", ", "), (r"&", " 그리고 "),
]


def _sino(n: str) -> str:
    """정수를 한자어 수 읽기로(4→사, 12→십이, 0→영, 2500→이천오백)"""
    n = int(n.replace(",", ""))
    if n == 0:
        return "영"
    d, out = "영일이삼사오육칠팔구", ""
    for unit_big, name in ((10 ** 8, "억"), (10 ** 4, "만"), (1, "")):
        q = (n // unit_big) % 10000
        if not q:
            continue
        part = ""
        for v, u in ((1000, "천"), (100, "백"), (10, "십"), (1, "")):
            k = (q // v) % 10
            if k:
                part += ("" if k == 1 and u else d[k]) + u
        out += ("" if part == "일" and name == "만" else part) + name
    return out


_SOUND = [  # 귀로 들을 때만 쓰는 소리 표기(원고에는 원래 표기 그대로). 예: 영장→영짱
    (r"5\s?[·.]\s?18", "오일팔"), (r"4\s?[·.]\s?19", "사일구"), (r"6\s?[·.]\s?25", "육이오"),
    (r"12\s?[·.]\s?12", "십이 십이"), (r"12\s?[·.]\s?3(?!\d)", "십이 삼"), (r"3\s?[·.]\s?1절", "삼일절"),
    (r"(\d+)\s?·\s?(\d+)", r"\1 \2"),                                # 그 밖의 숫자·숫자
    # 조직 이름은 붙여 읽기: '내란 특별수사본부'를 띄어 읽으면 어색함(11.27. 지적) — 원고 글은 그대로
    (r"(내란|비상계엄|계엄|채상병|김건희|순직해병|마약|전세사기|합동)\s+(특별수사본부|특수본|합동수사본부|합수본|수사본부|특별수사단|특수단|특검|특별검사)",
     r"\1\2"),
    (r"(서울중앙|서울남부|서울북부|서울동부|서울서부|의정부|서울|부산|대구|광주|대전|수원|인천|울산|창원|춘천|청주|전주|제주)"
     r"(지방중수청|중수청|지방공소청|공소청|고등공소청)", r"\1 \2"),         # 대전중수청 → 대전 중수청
    # 천 단위 쉼표: '2,500만'을 엔진이 '이, 오백만'처럼 쉼표에서 끊어 읽어 쉼표를 없앰(11.26. 지적)
    (r"(?<=\d),(?=\d{3}(?!\d))", ""),
    # 조·억·만 앞 숫자는 한글 소리로(5억 2500만 원 → 오억 이천오백만 원)
    (r"(?<![\d.])(\d+)(?=\s?(?:조|억|만))",
     lambda m: "" if m.group(1) == "1" and m.string[m.end():].lstrip().startswith("만") else _sino(m.group(1))),   # 1만 → 만
    (r"(?<![\d.])(\d+)(?=\s?(?:퍼센트|%))", lambda m: _sino(m.group(1))),   # 76퍼센트 → 칠십육퍼센트
    # 소수(4.2퍼센트 등): 숫자로 두면 음성 엔진이 '사 점 이 … 퍼센트'처럼 끊어 읽어 한글 소리로 붙여 씀(11.26. 지적)
    (r"(?<![\d.])(\d+)\.(\d+)(?![.\d])(?=\s?(?:퍼센트|%|배|포인트|%p|억|조|만|명|건|도|개월|년|kg|㎏|km|시간|초|분))",
     lambda m: _sino(m.group(1)) + "점" + "".join("영일이삼사오육칠팔구"[int(c)] for c in m.group(2))),
    (r"\b([A-Z])\s?씨", lambda m: {"A": "에이", "B": "비", "C": "씨", "D": "디", "E": "이", "F": "에프", "G": "지", "H": "에이치", "K": "케이", "L": "엘", "M": "엠", "P": "피", "S": "에스", "Y": "와이"}.get(m.group(1), m.group(1)) + " 씨"),
    (r"(수사|형사|조사|감사|기획)과(?![가-힣]*과)", r"\1꽈"),             # 수사과 → 수사꽈 (된소리)
    (r"(수사|형사|조사|감사|기획)과(?=장)", r"\1꽈"),                    # 수사과장 → 수사꽈장
    (r"(도매|소매)가(?!격)", r"\1까"),                                  # 도매가·소매가(가격) → 도매까·소매까 (조사 '가'로 읽히지 않게)
    (r"NDFAS", "엔디파스"), (r"KOLAS", "콜라스"), (r"(?<![A-Za-z])AI(?![A-Za-z])", "에이아이"), (r"(?<![A-Za-z])CCTV(?![A-Za-z])", "씨씨티비"),
    (r"(?<![A-Za-z])TF(?![A-Za-z])", "티에프"), (r"(?<![A-Za-z])SNS(?![A-Za-z])", "에스엔에스"), (r"(?<![A-Za-z])MOU(?![A-Za-z])", "업무협약"),
    # ── 한자어 접미사 된소리(표준 발음). 표기 그대로 두면 음성이 평음으로 읽는 말들 ──
    (r"영장(?!류)", "영짱"),                                                   # 영장·체포영장·압수수색영장
    (r"(고소|고발|공소|소환|임명|위촉|초청|경고)장", r"\1짱"),
    (r"(수사|기소|공소|소추|지휘|청구|직|인|관할|인사|재량|사법|입법|행정|검찰|경찰|통제|감독|지시|종결|개시|"
     r"선거|소유|저작|재산|참정|투표|기본|변호인선임|접견교통|접견|방어|항고|구속|체포|정|주|집|특|여)권(?!한|력|리|고)", r"\1꿘"),
    (r"(\d|한|두|세|네|다섯|여섯|일곱|여덟|아홉|열|몇|여러|수십|수백|수천)\s?건(?!설|강|물|축|전|국|재|배)", r"\1껀"),
    (r"(가능|필요|위법|정당|상당|신빙|실효|독립|중립|전문|공정|타당|적법|합법|개연|합리|비례|보충|객관|중대|구체|긴급|효율|필수)성", r"\1썽"),
    (r"법제", "법쩨"),
    (r"(소송|청|조직|선거|자금|금지|헌|형|민|상|불|합|위|적|탈|편)법(?![무령률리관원정안제인조규학치익])", r"\1뻡"),
    (r"(쟁|문제|공통|차이|유사|의문|접|시|관|허|요|장|단|거|기|종)점(?!검|수|령|유|포)", r"\1쩜"),
    (r"대가(?=성|를|로|는|도|와|[\s,.])", "대까"),
    (r"(자|성|인|규|품|합|결|실|적)격(?!렬|하|변|투|식|차|전|돌|려)", r"\1껵"),
    # ── 과(課): 앞 글자에 받침이 없거나 ㄱ받침이라 조사 '과'와 헷갈리지 않는 부서명만 ──
    (r"(총무|인사|법무|법쩨|특수|홍보|회계|통계|마약조직범죄|정책|강력)과(?!학|거|목|정|세|오|잉|제)", r"\1꽈"),
    (r"(?<![가-힣])(성|효)과", r"\1꽈"),
    # ── 가(價): 조사 '가'와 헷갈리지 않게 앞이 단어 경계인 것만 ──
    (r"(감정|낙찰|분양|최저|최고|상한|하한|평균|기준|취득|장부|매입|공급|매매|판매|실거래|공시)가(?!격)", r"\1까"),
    (r"(?<![가-힣])(시|정|원|단|물)가(?!격)", r"\1까"),
]


def pronounce(text: str, sound: bool = False) -> str:
    """sound=False: 원고(글)용 — 한자·괄호·시간 표기 정리만. sound=True: 음성용 — 거기에 소리 나는 대로 고침(_SOUND)"""
    if sound:
        pairs = [x.split("=", 1) for x in re.split(r"[,\n]", AUDIO_DICT) if "=" in x]
        for a, b in pairs:                         # 사용자 사전 먼저
            if a.strip():
                text = text.replace(a.strip(), b.strip())
    for pat, sub in _PRON:
        text = re.sub(pat, sub, text)
    if sound:
        for pat, sub in _SOUND:
            text = re.sub(pat, sub, text)
    return re.sub(r"\s*,\s*,", ",", text)


def tidy_summary(t: str) -> str:
    """AI 요약(개조식)을 대체 원고용 문장으로: 글머리 '-' 없애고, '(제목 기준…)' 같은 메모 삭제, 줄마다 문장으로"""
    t = re.sub(r"\((?:[^)]*(?:기준|본문|확인|추정)[^)]*)\)", "", t)
    lines = [re.sub(r"^\s*[-*•·]\s*", "", x).strip() for x in t.split("\n")]
    lines = [x if re.search(r"[.!?]$", x) else x + "." for x in lines if x]
    return " ".join(lines)


def speakable(text: str, sound: bool = False) -> str:
    text = pronounce(polite(text), sound)
    text = re.sub(r"(?m)^\s*(#+|[-*•]|\d+[.)])\s+", "", text).replace("**", "")
    text = re.sub(r"[\[(（]\s*(단독|속보|종합|사설|칼럼|기고|포토|영상)\s*[\])）]", " ", text)   # 머리표는 읽지 않음
    text = re.sub(r"[\"“”‘’'「」『』]", "", text)                 # 따옴표는 붙여서('이드러그'도 → 이드러그도)
    text = re.sub(r"[\[\]【】〈〉<>]", " ", text)
    text = re.sub(r"(?<!\d)·|·(?!\d)", " ", text).replace("→", ", ").replace("…", ", ")
    return re.sub(r"[ \t]+", " ", text).strip()


SENT_GAP = float(os.getenv("AUDIO_SENT_GAP") or "0.35")   # 문장 사이 쉼(초)
PARA_GAP = float(os.getenv("AUDIO_PARA_GAP") or "0.9")    # 문단(사안) 사이 쉼(초)
COMMA_GAP = float(os.getenv("AUDIO_COMMA_GAP") or "0.18")  # 쉼표 쉼(초). 0이면 쉼표에서 나누지 않음


def _clauses(sent: str) -> list:
    """문장을 쉼표 단위로 나눔. 너무 짧은 조각(6자 미만)은 앞뒤와 합쳐 어색하게 끊기지 않게 함"""
    if COMMA_GAP <= 0:
        return [sent]
    parts = re.split(r"(?<=,)\s+", sent)
    if sum(1 for x in parts[:-1] if len(x) <= 12) >= 2:   # 회사·기관 이름 나열 등은 엔진이 자연스럽게 읽게 통째로
        return [sent]
    out = []
    for part in parts:
        if out and (len(out[-1]) < 6 or len(part) < 6):
            out[-1] += " " + part
        else:
            out.append(part)
    return out


def _google_by_sentence(paras: list, voice: str, path: str) -> str:
    """문장마다 따로 읽혀서 사이에 정해진 길이의 무음을 넣음.
    한 번에 긴 글을 보내면 음성 엔진이 마침표·문단을 무시하고 이어 읽거나 엉뚱한 곳에서 쉬는 일이 있어,
    마침표와 문단 경계의 쉼을 프로그램이 직접 보장한다(쉼표도 조각으로 나눠 짧은 쉼을 직접 넣음)"""
    import base64
    import io
    import subprocess
    import wave
    sr, pcm = 24000, bytearray()
    for pi, p in enumerate(paras):
        sents = [x.strip() for x in re.split(r"(?<=[.?!])\s+", p) if x.strip()]
        for si, sent in enumerate(sents):
            parts = _clauses(sent)
            for ci, part in enumerate(parts):
                r = requests.post(
                    "https://texttospeech.googleapis.com/v1/text:synthesize",
                    params={"key": GOOGLE_TTS_KEY},
                    json={"input": {"text": part}, "voice": {"languageCode": "ko-KR", "name": voice},
                          "audioConfig": {"audioEncoding": "LINEAR16", "sampleRateHertz": sr,
                                          "speakingRate": AUDIO_RATE}},
                    timeout=60,
                )
                if not r.ok:
                    raise RuntimeError(f"구글 음성 오류 {r.status_code}: {r.text[:200]}")
                with wave.open(io.BytesIO(base64.b64decode(r.json()["audioContent"]))) as w:
                    pcm += w.readframes(w.getnframes())
                if ci < len(parts) - 1:
                    pcm += b"\x00\x00" * int(sr * COMMA_GAP)
            last = si == len(sents) - 1
            if not (last and pi == len(paras) - 1):
                pcm += b"\x00\x00" * int(sr * (PARA_GAP if last else SENT_GAP))
    wav = path[:-4] + ".wav"
    with wave.open(wav, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(bytes(pcm))
    ff = _ffmpeg()
    if ff:
        subprocess.run([ff, "-y", "-v", "error", "-i", wav, "-b:a", "96k", path], check=True, capture_output=True, timeout=120)
    else:
        os.replace(wav, path)
    return f"구글 {voice} 문장별 쉼 쉼표{COMMA_GAP}/문장{SENT_GAP}/문단{PARA_GAP}초 속도{AUDIO_RATE}"


def tts_mp3(text: str, path: str) -> str:
    """원고 → mp3. GOOGLE_TTS_KEY가 있으면 구글, 없으면 edge-tts(무료). 쓴 엔진 이름을 돌려줌"""
    if GOOGLE_TTS_KEY:
        import base64
        voice = AUDIO_VOICE or "ko-KR-Neural2-A"
        paras = [p.strip() for p in re.split(r"\n\s*\n|\n", text) if p.strip()]
        pieces = []                                # 긴 문단은 문장 단위로 쪼갬(구글은 한 번에 5,000바이트까지)
        for p in paras:
            if len(p.encode()) <= 3500:
                pieces.append(p)
                continue
            cur = ""
            for sent in re.split(r"(?<=[.?!])\s+", p):
                if cur and len((cur + " " + sent).encode()) > 3500:
                    pieces.append(cur)
                    cur = sent
                else:
                    cur = (cur + " " + sent).strip()
            if cur:
                pieces.append(cur)
        chunks, cur = [], []                       # 문단 경계에서만 나눠 보냄(억양이 중간에 끊기지 않게)
        for p in pieces:
            if cur and len(" [pause long] ".join(cur + [p]).encode()) > 4200:
                chunks.append(cur)
                cur = [p]
            else:
                cur.append(p)
        if cur:
            chunks.append(cur)
        # 쉼 표시([pause long])는 Chirp 3 HD가 그 자리에 '음·어' 같은 군말을 넣는 경우가 있어 기본은 끔(AUDIO_PAUSE=on으로 켜기)
        use_markup = "chirp3" in voice.lower() and (os.getenv("AUDIO_PAUSE") or "off").strip().lower() == "on"
        if (os.getenv("AUDIO_SPLIT") or "on").strip().lower() != "off" and not use_markup:
            return _google_by_sentence(paras, voice, path)
        with open(path, "wb") as f:
            for c in chunks:
                for mode in (["markup", "text"] if use_markup else ["text"]):
                    body = " [pause long] ".join(c) if mode == "markup" else "\n\n".join(c)
                    r = requests.post(
                        "https://texttospeech.googleapis.com/v1/text:synthesize",
                        params={"key": GOOGLE_TTS_KEY},
                        json={"input": {mode: body}, "voice": {"languageCode": "ko-KR", "name": voice},
                              "audioConfig": {"audioEncoding": "MP3", "speakingRate": AUDIO_RATE}},
                        timeout=90,
                    )
                    if r.ok:
                        break
                    print(f"[구글 음성 {mode} 실패] {r.status_code}: {r.text[:150]}", file=sys.stderr)
                if not r.ok:
                    raise RuntimeError(f"구글 음성 오류 {r.status_code}: {r.text[:200]}")
                f.write(base64.b64decode(r.json()["audioContent"]))
        return f"구글 {voice}" + (" 쉼표시" if use_markup and mode == "markup" else "") + f" 속도{AUDIO_RATE}"
    import asyncio
    import edge_tts
    voice = AUDIO_VOICE or "ko-KR-SunHiNeural"
    rate = f"{round((AUDIO_RATE - 1) * 100):+d}%"
    asyncio.run(edge_tts.Communicate(re.sub(r"\n\s*\n", "\n", text), voice, rate=rate).save(path))
    if os.path.getsize(path) < 1000:
        raise RuntimeError("음성 파일이 비어 있음")
    return f"edge {voice}"



# ── 배경음악: 외부 음원 없이 코드로 직접 합성(저작권 걱정 없음) ──────────────
# AUDIO_BGM_STYLE: 1 속보 시그널 · 2 수사 드라마 · 3 탐사보도 · 4 긴박 리포트 · 5 정보 브리핑
_SR = 22050


def _write_wav(path: str, data: list, sr: int = _SR) -> None:
    import struct
    import wave
    peak = max(1e-9, max(abs(x) for x in data))
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(b"".join(struct.pack("<h", int(x / peak * 0.9 * 32767)) for x in data))


def _mf(m: float) -> float:
    return 440.0 * 2 ** ((m - 69) / 12)


_SINE = (1.0, 0.25, 0.08)
_SAW = (1.0, 0.5, 0.33, 0.25, 0.2, 0.16, 0.12)
_SQR = (1.0, 0.0, 0.33, 0.0, 0.2, 0.0, 0.14)
_BRASS = (1.0, 0.7, 0.5, 0.35, 0.25, 0.15)


def _tone(buf, start, dur, m, amp, harm=_SINE, attack=0.005, decay=0.0, release=0.03, sr=_SR):
    import math
    i0, n = int(start * sr), int(dur * sr)
    w = 2 * math.pi * _mf(m) / sr
    hs = [(h + 1, a) for h, a in enumerate(harm) if a]
    for k in range(n):
        i = i0 + k
        if i >= len(buf):
            break
        t = k / sr
        env = min(1.0, t / attack) if attack else 1.0
        if decay:
            env *= math.exp(-t / decay)
        if t > dur - release:
            env *= max(0.0, (dur - t) / release)
        if env < 1e-4:
            continue
        buf[i] += amp * env * sum(a * math.sin(h * w * k) for h, a in hs)


def _kick(buf, start, amp, sr=_SR):
    import math
    i0, ph = int(start * sr), 0.0
    for k in range(int(0.3 * sr)):
        if i0 + k >= len(buf):
            break
        t = k / sr
        ph += 2 * math.pi * (45 + 100 * math.exp(-t / 0.025)) / sr
        buf[i0 + k] += amp * math.exp(-t / 0.11) * math.sin(ph)


def _noise(buf, start, amp, decay, rnd, dur=0.15, hp=True, sr=_SR):
    import math
    i0, prev = int(start * sr), 0.0
    for k in range(int(dur * sr)):
        if i0 + k >= len(buf):
            break
        x = rnd.random() * 2 - 1
        y, prev = (x - prev if hp else x), x
        buf[i0 + k] += amp * math.exp(-k / sr / decay) * y


def _snare(buf, start, amp, rnd):
    _noise(buf, start, amp, 0.07, rnd, dur=0.2)
    _tone(buf, start, 0.12, 55, amp * 0.6, attack=0.001, decay=0.04)


def _style(n: int):
    """(시작 시그널, 반복 반주) 두 목록을 돌려줌"""
    import random
    rnd = random.Random(n)
    if n == 1:      # 속보 시그널: 128BPM D단조, 16분음표 베이스 맥박 + 팀파니 + 스타카토 화음
        bpm, prog = 128, [(50, 53, 57), (46, 50, 53), (48, 52, 55), (50, 53, 57)]
    elif n == 2:    # 수사 드라마: 120BPM A단조, 째깍 초침 + 낮은 현악 스타카토 + 긴장 패드
        bpm, prog = 120, [(45, 48, 52), (41, 45, 48), (38, 41, 45), (40, 44, 47)]
    elif n == 3:    # 탐사보도: 132BPM E단조, 킥·하이햇 + 16분 아르페지오 + 저음 드론
        bpm, prog = 132, [(40, 43, 47), (36, 40, 43), (45, 48, 52), (47, 51, 54)]
    elif n == 4:    # 긴박 리포트: 140BPM C단조, 당김음 금관 스탭 + 8분 베이스 + 스네어
        bpm, prog = 140, [(48, 51, 55), (44, 48, 51), (51, 55, 58), (43, 47, 50)]
    else:           # 정보 브리핑: 124BPM G단조, 피아노 8분 반복 + 신스 베이스 + 가벼운 비트
        bpm, prog = 124, [(43, 46, 50), (39, 43, 46), (46, 50, 53), (41, 45, 48)]
    beat = 60.0 / bpm
    bar = 4 * beat
    # ── 반주(4마디 × 2바퀴) ──
    bars = prog * 2
    b = [0.0] * int(len(bars) * bar * _SR)
    for c, ch in enumerate(bars):
        t0 = c * bar
        root = ch[0] - 12
        if n == 1:
            for q in range(16):
                _tone(b, t0 + q * beat / 4, beat / 4 * 0.8, root, 0.30 if q % 4 == 0 else 0.18, _SAW, decay=0.08)
            for q in (0, 2):
                _tone(b, t0 + q * beat, 0.6, root - 12, 0.5, _SINE, attack=0.002, decay=0.35)   # 팀파니
            for q in (1.5, 3.5):
                for m in ch:
                    _tone(b, t0 + q * beat, 0.18, m + 12, 0.07, _BRASS, decay=0.08)
            for q in range(8):
                _noise(b, t0 + q * beat / 2, 0.05, 0.02, rnd, dur=0.04)
        elif n == 2:
            for q in range(16):
                _noise(b, t0 + q * beat / 4, 0.09 if q % 2 == 0 else 0.04, 0.008, rnd, dur=0.02)   # 초침
            pat = [0, 0, 3, 0, 0, 0, 3, 5]
            for q, d in enumerate(pat):
                _tone(b, t0 + q * beat / 2, beat / 2 * 0.6, ch[0] + d - 12, 0.22, _SAW, decay=0.12)
            for m in ch:
                _tone(b, t0, bar, m + 12, 0.05, _SINE, attack=0.4, release=0.2)
            _tone(b, t0, bar, ch[0] + 23, 0.025, _SINE, attack=0.6, release=0.3)               # 높은 긴장음
            _kick(b, t0, 0.35)
        elif n == 3:
            for q in range(4):
                _kick(b, t0 + q * beat, 0.5)
                _noise(b, t0 + q * beat + beat / 2, 0.10, 0.03, rnd, dur=0.06)
            arp = [ch[0], ch[1], ch[2], ch[2] + 5]
            for q in range(16):
                _tone(b, t0 + q * beat / 4, beat / 4 * 0.7, arp[q % 4] + 12, 0.10, _SQR, decay=0.07)
            _tone(b, t0, bar, root, 0.20, _SAW, attack=0.05, release=0.05)
        elif n == 4:
            for q in range(8):
                _tone(b, t0 + q * beat / 2, beat / 2 * 0.7, root, 0.25, _SAW, decay=0.1)
            for q in (0, 1.5, 3, 3.5):
                for m in ch:
                    _tone(b, t0 + q * beat, 0.16, m + 12, 0.08, _BRASS, decay=0.07)
            _kick(b, t0, 0.5)
            _kick(b, t0 + 2.5 * beat, 0.4)
            for q in (1, 3):
                _snare(b, t0 + q * beat, 0.18, rnd)
            for q in range(8):
                _noise(b, t0 + q * beat / 2, 0.04, 0.015, rnd, dur=0.03)
        else:
            pat = [ch[0] + 12, ch[2] + 12, ch[1] + 12, ch[2] + 12] * 2
            for q, m in enumerate(pat):
                _tone(b, t0 + q * beat / 2, beat / 2, m, 0.12, _SINE, decay=0.25)               # 피아노 느낌
            for q in (0, 1.5, 2, 3):
                _tone(b, t0 + q * beat, beat * 0.45, root, 0.22, _SQR, decay=0.15)
            _kick(b, t0, 0.35)
            _kick(b, t0 + 2 * beat, 0.3)
            for q in range(8):
                _noise(b, t0 + q * beat / 2, 0.035, 0.02, rnd, dur=0.04)
    # ── 시작 시그널(약 3초) ──
    j = [0.0] * int(3.4 * _SR)
    ch = prog[0]
    if n in (1, 4):          # 빠른 상승 스탭 3번 + 화음 히트
        for i, m in enumerate((ch[0], ch[1], ch[2])):
            for x in (0, 12):
                _tone(j, i * 0.12, 0.1, m + x, 0.18, _BRASS, decay=0.06)
        for m in ch + (ch[0] + 12, ch[0] - 12, ch[0] - 24):
            _tone(j, 0.42, 2.6, m, 0.13, _BRASS, attack=0.004, decay=0.7, release=0.4)
        _tone(j, 0.42, 2.0, ch[0] - 24, 0.5, _SINE, attack=0.002, decay=0.5)
        _noise(j, 0.42, 0.10, 0.2, rnd, dur=0.6, hp=False)
    elif n == 2:             # 낮은 '둥-' 두 번 + 긴장 화음
        for t in (0.0, 0.6):
            _tone(j, t, 0.9, ch[0] - 24, 0.6, _SINE, attack=0.002, decay=0.4)
            _tone(j, t, 0.9, ch[0] - 12, 0.25, _SAW, attack=0.002, decay=0.25)
        for m in (ch[0], ch[0] + 1, ch[2]):
            _tone(j, 1.2, 2.1, m + 12, 0.08, _SAW, attack=0.3, release=0.5)
    elif n == 3:             # 빠른 아르페지오 상승 + 킥
        for i in range(8):
            _tone(j, i * 0.07, 0.12, ch[i % 3] + 12 * (i // 3), 0.15, _SQR, decay=0.06)
        _kick(j, 0.6, 0.7)
        for m in ch:
            _tone(j, 0.6, 2.5, m + 12, 0.1, _SAW, attack=0.01, decay=0.8, release=0.4)
    else:                    # 피아노 3음 모티프 + 화음
        for i, m in enumerate((ch[2] + 12, ch[1] + 12, ch[0] + 24)):
            _tone(j, i * 0.18, 0.5, m, 0.3, _SINE, decay=0.3)
        _kick(j, 0.6, 0.5)
        for m in ch + (ch[0] + 12,):
            _tone(j, 0.6, 2.6, m + 12, 0.15, _SINE, attack=0.003, decay=0.9, release=0.4)
        _tone(j, 0.6, 2.6, ch[0] - 12, 0.3, _SQR, decay=0.6)
    return j, b


def make_bgm(folder: str, style: int = 1):
    j, b = _style(style)
    jp, bp = os.path.join(folder, f"bgm_intro{style}.wav"), os.path.join(folder, f"bgm_bed{style}.wav")
    _write_wav(jp, j)
    _write_wav(bp, b)
    return jp, bp


def _ffmpeg() -> str:
    """ffmpeg 실행 파일 위치(없으면 pip의 imageio-ffmpeg에 든 것)"""
    import shutil
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return ""


def _duration(ff: str, path: str) -> float:
    import subprocess
    err = subprocess.run([ff, "-hide_banner", "-i", path], capture_output=True, text=True, timeout=60).stderr
    h, m, sec = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", err).groups()
    return int(h) * 3600 + int(m) * 60 + float(sec)


def pick_music(folder: str, now: dt.datetime) -> str:
    """사용자가 정한 음원(URL 또는 bgm 폴더) 중 이번 브리핑에 쓸 것 하나. 없으면 ''"""
    import glob
    urls = [u.strip() for u in AUDIO_BGM_URL.split(",") if u.strip().startswith("http")]
    files = sorted(glob.glob("bgm/*.mp3") + glob.glob("bgm/*.m4a") + glob.glob("bgm/*.wav"))
    items = urls + files
    if not items:
        return ""
    pick = items[(now.toordinal() * 2 + (now.hour >= 12)) % len(items)]   # 브리핑마다 돌아가며
    if not pick.startswith("http"):
        return pick
    try:
        r = requests.get(pick, headers=UA, timeout=60)
        r.raise_for_status()
        if len(r.content) < 20000:
            raise ValueError(f"파일이 너무 작음({len(r.content)}바이트) — 음원 직접 주소가 아닐 수 있음")
        path = os.path.join(folder, "bgm_download" + os.path.splitext(urlparse(pick).path)[1][:5])
        with open(path, "wb") as f:
            f.write(r.content)
        return path
    except Exception as e:
        print(f"[배경음악 내려받기 실패] {pick[:80]} {type(e).__name__}: {e}", file=sys.stderr)
        return ""


def add_bgm(voice: str, now: dt.datetime = None) -> str:
    """음성 앞에 시작 음, 밑에 작은 반주, 끝에 반주가 잠깐 커졌다 사라지게. 실패하면 음성만"""
    import subprocess
    ff = _ffmpeg()
    if not AUDIO_BGM or not ff:
        return voice
    try:
        music = pick_music(os.path.dirname(os.path.abspath(voice)), now or dt.datetime.now(KST))
        if music:                                  # 외부 음원: 처음 3초는 크게 → 목소리 밑에 작게 → 끝에 다시 커졌다 사라짐
            try:
                dur = _duration(ff, voice)
                lead, tail = 3.0, 4.0
                total, end = lead + dur + tail, lead + dur
                out = voice[:-4] + "_bgm.mp3"
                vol = (f"if(lt(t,{lead - 0.8:.2f}),0.6,if(lt(t,{lead:.2f}),0.6-0.48*(t-{lead - 0.8:.2f})/0.8,"
                       f"0.12+0.2*clip((t-{end:.2f})/1.0,0,1)))")
                fc = (f"[0:a]aresample=44100,aformat=channel_layouts=mono,adelay={int(lead * 1000)}:all=1[v];"
                      f"[1:a]aresample=44100,aformat=channel_layouts=mono,atrim=0:{total:.2f},asetpts=PTS-STARTPTS,"
                      f"volume='{vol}':eval=frame,afade=t=in:st=0:d=0.5,afade=t=out:st={total - 2.5:.2f}:d=2.5[b];"
                      f"[v][b]amix=inputs=2:duration=longest:normalize=0,alimiter=limit=0.95[o]")
                subprocess.run([ff, "-y", "-v", "error", "-i", voice, "-stream_loop", "-1", "-i", music,
                                "-filter_complex", fc, "-map", "[o]", "-t", f"{total:.2f}", "-ac", "1", "-b:a", "96k", out],
                               check=True, capture_output=True, timeout=180)
                if os.path.getsize(out) > 1000:
                    BGM_USED[0] = "지정 음원" if AUDIO_BGM_URL else os.path.basename(music)
                    print(f"배경음악: {BGM_USED[0]} 사용")
                    return out
            except Exception as e:
                print(f"[지정 배경음악 실패 → 기본 음악 사용] {type(e).__name__}: {str(e)[:150]}", file=sys.stderr)
        folder = os.path.dirname(os.path.abspath(voice))
        jp, bp = make_bgm(folder, AUDIO_BGM_STYLE)
        dur = _duration(ff, voice)
        lead, tail = 2.8, 4.0
        total = lead + dur + tail
        out = voice[:-4] + "_bgm.mp3"
        fc = (f"[0:a]aresample=44100,aformat=channel_layouts=mono,adelay={int(lead * 1000)}:all=1[v];"
              f"[1:a]aresample=44100,volume=0.55[j];"
              f"[2:a]aresample=44100,atrim=0:{total:.2f},"
              f"volume='0.10+0.12*clip((t-{lead + dur:.2f})/0.8,0,1)':eval=frame,"
              f"afade=t=in:st=1.5:d=1.5,afade=t=out:st={total - 2.5:.2f}:d=2.5[b];"
              f"[v][j][b]amix=inputs=3:duration=longest:normalize=0,alimiter=limit=0.95[o]")
        subprocess.run([ff, "-y", "-v", "error", "-i", voice, "-i", jp, "-stream_loop", "-1", "-i", bp,
                        "-filter_complex", fc, "-map", "[o]", "-t", f"{total:.2f}", "-ac", "1", "-b:a", "96k", out],
                       check=True, capture_output=True, timeout=180)
        if os.path.getsize(out) > 1000:
            BGM_USED[0] = f"기본 음악 {AUDIO_BGM_STYLE}번"
            return out
    except Exception as e:
        print(f"[배경음악 생략] {type(e).__name__}: {str(e)[:150]}", file=sys.stderr)
    return voice


def to_voice(mp3: str) -> str:
    """텔레그램 '음성 메시지'(ogg/opus)로 변환. 음악 파일(sendAudio)은 끝나면 채팅방의 이전 음악이
    이어서 재생되지만, 음성 메시지는 더 새로운 음성 메시지로만 넘어가므로 최신 브리핑은 끝나면 멈춤"""
    import subprocess
    ff = _ffmpeg()
    if not ff:
        return ""
    out = mp3[:-4] + ".ogg"
    try:
        subprocess.run([ff, "-y", "-v", "error", "-i", mp3, "-ac", "1", "-ar", "48000",
                        "-c:a", "libopus", "-b:a", "48k", "-application", "audio", out],
                       check=True, capture_output=True, timeout=180)
        return out if os.path.getsize(out) > 1000 else ""
    except Exception as e:
        print(f"[음성 메시지 변환 실패] {type(e).__name__}: {str(e)[:150]}", file=sys.stderr)
        return ""


AUDIO_FORMAT = (os.getenv("AUDIO_FORMAT") or "voice").strip().lower()   # voice(음성 메시지) | music(음악 파일)


def send_audio(path: str, caption: str, title: str, fmt: str = "") -> int:
    """fmt=music: 음악 파일로 보냄 → 위쪽 재생 막대를 누르면 큰 재생 화면(⏮ 처음으로·진행 막대)이 열림.
    대신 끝나면 채팅방의 다른 음악 파일이 이어 재생될 수 있음. voice: 음성 메시지(끝나면 멈춤)"""
    ogg = "" if (fmt or AUDIO_FORMAT) == "music" else to_voice(path)
    if ogg:                                        # 음성 메시지로 발송(재생이 끝나면 멈춤)
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendVoice"
        field, fpath, mime = "voice", ogg, "audio/ogg"
        data = {"chat_id": TG_CHAT.strip(), "caption": caption, "parse_mode": "HTML"}
    else:                                          # 변환이 안 되면 예전처럼 음악 파일로
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendAudio"
        field, fpath, mime = "audio", path, "audio/mpeg"
        data = {"chat_id": TG_CHAT.strip(), "caption": caption, "parse_mode": "HTML",
                "title": title, "performer": "중수청 브리핑"}
    for _ in range(2):
        with open(fpath, "rb") as f:
            r = requests.post(url, data=data, files={field: (os.path.basename(fpath), f, mime)}, timeout=120)
        if r.ok:
            try:
                return int(r.json()["result"]["message_id"])
            except Exception:
                return 0
        print(f"[텔레그램 음성 오류] {r.status_code}: {r.text[:200]}", file=sys.stderr)
        data.pop("parse_mode", None)               # 서식 문제일 수 있어 서식 없이 다시
        data["caption"] = html.unescape(re.sub(r"</?b>", "", caption))
    r.raise_for_status()


def send_script(script: str, reply_to: int, head: str) -> None:
    """음성에 실제로 넣은 원고를 문단 번호와 함께 글로 보냄(음성 메시지에 답장 형태, 접힌 상태로)
    → 들으면서 '3번 문단 ○○ 발음 이상'처럼 바로 짚을 수 있게"""
    paras = [p.strip() for p in re.split(r"\n\s*\n|\n", script) if p.strip()]
    body = "\n\n".join(f"{n}) {html.escape(p)}" for n, p in enumerate(paras, 1))
    parts, cur = [], ""
    for blk in body.split("\n\n"):                # 텔레그램 4,096자 제한 대비 문단 단위로 나눔
        if cur and len(cur) + len(blk) > 3500:
            parts.append(cur)
            cur = blk
        else:
            cur = (cur + "\n\n" + blk) if cur else blk
    if cur:
        parts.append(cur)
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    for i, part in enumerate(parts):
        text = (head if i == 0 else "") + f"<blockquote expandable>{part}</blockquote>"
        payload = {"chat_id": TG_CHAT.strip(), "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        if reply_to:
            payload["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        r = requests.post(url, json=payload, timeout=30)
        if not r.ok:                               # 접기 기능이 안 되면 그냥 글로
            print(f"[원고 발송 오류] {r.status_code}: {r.text[:150]}", file=sys.stderr)
            payload.pop("parse_mode", None)
            payload["text"] = html.unescape(re.sub(r"</?(b|blockquote[^>]*)>", "", text))
            requests.post(url, json=payload, timeout=30)


# ── 원문 대조 검증(브리핑·카드를 보내기 전) ────────────────────────────
def _excerpts(g: list, n: int = 2, size: int = 1200) -> str:
    """대본·카드 AI에 함께 넘길 기사 본문 앞부분"""
    out = []
    for x in g[:n]:
        b = (x.get("body") or "").strip()
        if b:
            out.append(f"본문 발췌({x['press']}): {b[:size]}")
    return "\n".join(out) or "본문 발췌: (없음 - 제목과 요약만 있음)"


def _evidence(groups: list) -> str:
    """검증 근거: 기사 제목과 원문 앞부분(AI 요약은 근거로 쓰지 않음. 원문을 보관하기 전 기록만 예외)"""
    blocks = []
    for n, g in enumerate(groups, 1):
        for x in g[:4]:
            if "body" not in x:                    # 원문 보관 기능 이전 기록
                ev = f"(원문 없음, AI 요약 참고) {x.get('summary') or ''}"
            else:
                ev = x["body"] or "(본문 없음 - 근거는 제목뿐)"
            blocks.append(f"[사안 {n}] ({x['press']}) 제목: {x['title']}\n원문: {ev}")
    return "\n\n".join(blocks)


_SELF_OK = re.compile(r"일치(?!하지|\s*않)|문제\s*없|맞음|맞으나|맞는|동일|같은 취지|취지(?:는|가)? 같|생략")
# 검증이 문장을 너무 많이 빼면(검증 쪽 오류일 가능성) 빼지 않고 의심 표시만
VERIFY_MAX_DROP = float(os.getenv("VERIFY_MAX_DROP") or "0.3")


def verify_claims(claims: list, groups: list, now: dt.datetime):
    """claims=[(번호, 사안번호 또는 0, 문장)] → {번호: 이유} (문제 문장만). 검증 자체가 안 되면 None"""
    if not claims:
        return {}
    if not ANTHROPIC_KEY:
        return None
    lines = "\n".join(f"{i}. " + (f"[사안 {k}] " if k else "") + t for i, k, t in claims)
    prompt = (
        "너는 공무원 내부 언론보도 브리핑의 사실 검증 담당자다. 아래 [검증할 문장]을 [기사 원문]과 [기본 사실]에 대조해 "
        "'틀린 문장'만 찾아라. 문장을 쓴 사람은 따로 있다.\n"
        "틀린 문장으로 표시하는 경우(이것만):\n"
        "- 원문과 반대되거나 다른 사실: 숫자·날짜·기관명·소속·직함·주체가 원문과 다름, 원문에 없는 인과관계를 만듦, "
        "이미 일어난 일을 미래로(또는 계획을 확정으로) 바꿈, 제목의 비유를 사실로 바꿈, 의견·추측을 사실로 단정.\n"
        "- 원문 어디에도 없는 새 사실(숫자·사건·인물·발언)을 덧붙임.\n"
        "- 기본 사실과 어긋남. 특히 중수청·경찰청의 소속이나 행안부의 관할이 새로 바뀌는 것처럼 쓴 문장('관할하게 됩니다', '산하가 됩니다')은 "
        "원문에 그렇게 적혀 있어도 왜곡이다(둘 다 원래 행안부 소속).\n"
        "- 원문에 없는 전망·평가를 덧붙임(예: '~이 중요해질 전망입니다', '과제로 떠올랐습니다'). 원문에 그 전망이 누구의 말로 있으면 틀린 것이 아님.\n"
        "틀린 것이 아닌 경우(표시 금지):\n"
        "- 원문 정보를 줄이거나 생략함(예: 기간·조건을 빼고 핵심만 말함). 빠진 정보는 오류가 아니다.\n"
        "- 표현만 다르고 뜻이 같음, 쉬운 말로 풀어씀, 여러 문장을 합침, 존댓말 어미, 넘김말.\n"
        "- 원문과 일치함. 일치하면 절대 표시하지 않는다.\n"
        "- 확신이 없으면 표시하지 않는다.\n"
        "- 사안 번호가 붙은 문장은 그 사안 원문으로, 번호가 없으면 전체 원문으로 판단한다.\n"
        "출력: 틀린 문장만 JSON 배열. 각 항목에 문장 번호 n, 그 문장의 앞 10글자 head, 판정 v('왜곡' 또는 '근거없음'), "
        "원문에서 어긋나는 부분을 그대로 옮긴 src(근거없음이면 ''), 이유 why(40자 이내).\n"
        "[{\"n\": 3, \"head\": \"이에 따라 경찰\", \"v\": \"왜곡\", \"src\": \"행안부는 경찰청과 중수청을 관할한다\", \"why\": \"지명이 소속을 바꾼다고 씀\"}]\n"
        "틀린 문장이 없으면 []만 출력.\n\n"
        f"오늘: {now:%Y-%m-%d}\n[기본 사실]\n{FACTS}\n\n[기사 원문]\n{_evidence(groups)}\n\n[검증할 문장]\n{lines}")
    text = ask_claude(prompt, 4000, model=VERIFY_MODEL)
    by_n = {i: t for i, _, t in claims}
    try:
        data, _ = json.JSONDecoder().raw_decode(text[text.index("["):])
        out = {}
        for d in data:
            if not isinstance(d, dict) or str(d.get("v", "")).strip() not in ("왜곡", "근거없음"):
                continue
            n, why = int(d["n"]), str(d.get("why", ""))
            if n not in by_n:
                continue
            head = re.sub(r"\s+", "", str(d.get("head", "")))[:6]
            if head and head not in re.sub(r"\s+", "", by_n[n])[:20]:
                AUDIO_ERR.append(f"검증 번호 불일치 무시: {n}")   # 번호를 잘못 짚은 판정
                continue
            if _SELF_OK.search(why):               # 이유에 '일치·문제 없음'이라 적고 표시한 모순 판정은 무시
                continue
            out[n] = f"{d.get('v')}: {why[:60]}"
        return out
    except Exception as e:
        AUDIO_ERR.append(f"검증 실패: {type(e).__name__}: {str(e)[:150]}")
        return None


def _split_sents(p: str) -> list:
    return [x.strip() for x in re.split(r"(?<=[.?!])\s+", p) if x.strip()]


_PHOTO = re.compile(r"사진 기사|포토 기사|사진만 있|[\[【(]\s*(?:포토|사진|화보|포토뉴스)\s*[\]】)]")


def is_photo(x: dict) -> bool:
    """사진 한 장과 설명뿐인 기사(포토 기사) — 브리핑·카드에서 뺌"""
    return bool(_PHOTO.search(x.get("title", "") + " " + (x.get("summary") or "")))


_MARK = re.compile(r"^\s*\[\s*사안\s*(\d+)\s*\]\s*")
_LEAD = re.compile(r"^(?:(?:다음|마지막) 소식입니다[.,]?|이어서 전해 드립니다[.,]?|(?:먼저|이어서|한편|다음으로|마지막으로)(?:,|\s))\s*")


_INTRO_RE = re.compile(r"언론보도 (?:\S+ )?브리핑입니다")


FIXES: list = []                                   # 보내기 전 자동 교정 기록(점검 결과 한 줄에 씀)


def _plain(p: str, src: str = "") -> str:
    """'~에 그쳤습니다', '~에 불과했습니다'처럼 평가가 섞인 끝맺음을 있는 그대로('~였습니다')로 바꿈(지시문으로 막아도 자주 나옴)"""
    def sub(m):
        w = m.group(1)
        last = w[-1]
        batchim = "가" <= last <= "힣" and (ord(last) - 0xAC00) % 28 != 0
        return w + ("이었습니다" if batchim or last.isdigit() else "였습니다")
    p0 = p
    p = re.sub(r"(\S+?)(?:에|에 불과)\s*(?:그쳤습니다|불과했습니다)", sub, p)
    # 문장 첫머리의 인과 연결어는 원문에 그 말이 없으면 지움(AI가 관계를 지어내는 일이 잦음)
    for w in ("그래서", "따라서", "이 때문에", "이에 따라"):
        if w not in src:
            p = re.sub(r"(^|(?<=[.?!]\s))" + w + r",?\s+", r"\1", p)
    p = _fix_facts(p)
    p = re.sub(r"(\S+?)에 그치고 있습니다", lambda m: m.group(1) + "입니다", p)
    # '약 76퍼센트인 4억원을 ~했습니다' → '4억원을 ~했습니다. 전체의 약 76퍼센트입니다.'(귀로 듣기 어려운 숫자 겹치기)
    p = re.sub(r"(이 가운데 )?약 (\d+(?:\.\d+)?)퍼센트인 ([\d억만천,\s]+원)(을|를|은|는|이|가) ([^.]*?다)\.",
               lambda m: f"{m.group(1) or ''}{m.group(3)}{m.group(4)} {m.group(5)}. 전체의 약 {m.group(2)}퍼센트입니다.", p)
    # 기사에 있더라도 앵커가 단정하면 해설처럼 들리는 평가·전망 문장은 뺌(누가 한 말인지 밝힌 문장은 둠)
    sents = re.split(r"(?<=[.?!])\s+", p)
    keep = [x for x in sents if not (_EVAL_END.search(x) and not _SAID.search(x))]
    if keep:
        p = " ".join(keep)
    if p != p0:
        FIXES.append("표현")
    return p


_EVAL_END = re.compile(r"(떠올랐습니다|부상했습니다|도마에 올랐습니다|셈입니다|전망입니다|것으로 보입니다|중요해질 것입니다|"
                       r"드문 (?:발언|일|사례)입니다|이례적입니다|이례적인 일입니다)\.?$")
_SAID = re.compile(r"[고라]\s*(?:말했|밝혔|전했|내다봤|분석했|지적했|평가했|설명했|강조했|주장했)")


def _fix_facts(t: str) -> str:
    """기본 사실과 어긋나는 흔한 표현 교정: 중수청·경찰청은 원래 행안부 소속이라 '관할하게 됩니다'(새로 바뀜)가 아님
    (11.27. 아침: 기사 '관할하게 된 행안부'를 '관할하게 됩니다'로 옮김)"""
    t = re.sub(r"(관할|관장|감독)하게 됩니다", r"\1합니다", t)
    t = re.sub(r"(관할|관장|감독)하게 된", r"\1하는", t)
    return re.sub(r"(관할|관장|감독)하게 됨", r"\1함", t)


def _fix_today(p: str, g: list, now: dt.datetime) -> str:
    """기사 속 '오늘'을 그대로 옮긴 경우(11.26. 아침: 25일 증선위 의결을 '오늘 정례회의'로 읽음) 기사 작성일로 바꿈.
    그 사안의 기사가 모두 오늘 이전에 쓰였을 때만 바꿈"""
    days = []
    for x in g:
        try:
            pub = x["pub"] if isinstance(x.get("pub"), dt.datetime) else dt.datetime.fromisoformat(str(x.get("pub")))
            days.append(pub.astimezone(KST).date())
        except Exception:
            return p
    if not days or max(days) >= now.date():
        return p
    d = max(days)
    q = re.sub(r"오늘(?=\s)", f"{d.day}일", p)
    if q != p:
        FIXES.append("날짜")
    return q


def _parse_script(raw: str, intro_fixed: str = ""):
    """대본 → (첫머리, [(사안번호, 문단)], 맺음말).
    첫머리·맺음말은 위치가 아니라 내용으로 찾음(11.26. 아침: AI가 첫머리를 맨 뒤에 쓰고 첫 문단을 사안으로 시작해
    '사안 1'이 첫머리로 읽히고 넘김말이 한 칸씩 밀림). 첫머리는 프로그램 문구(intro_fixed)를 씀.
    '[사안 n]' 표시가 없으면 문단 순서로 번호를 붙임"""
    paras = [p.strip() for p in re.split(r"\n\s*\n", raw.strip()) if p.strip()]
    if len(paras) < 3:                             # 문단 구분이 없으면 줄 단위로
        paras = [p.strip() for p in raw.strip().split("\n") if p.strip()]
    intro, outro, body = "", "", []
    for p in paras:
        if not _MARK.match(p) and "브리핑이었습니다" in p:
            outro = p
            continue
        if not _MARK.match(p) and _INTRO_RE.search(p) and len(p) < 90:
            intro = intro or p
            continue
        m = _MARK.match(p)
        t = _MARK.sub("", p).strip()
        t = _LEAD.sub("", t, count=1).strip() or t   # AI가 넘김말을 썼으면 지우고 프로그램이 다시 붙임
        body.append((int(m.group(1)) if m else len(body) + 1, t))
    return (intro_fixed or intro), body, outro


def _transitions(intro: str, body: list, outro: str) -> str:
    """사안이 바뀌는 문단 앞에 넘김말을 프로그램이 직접 붙임(AI가 빠뜨리거나 검증에서 빠져도 항상 들어가게)"""
    starts = [i for i, (k, _) in enumerate(body) if i == 0 or body[i - 1][0] != k]
    out = []
    for i, (k, p) in enumerate(body):
        if len(starts) > 1 and i in starts:
            n = starts.index(i)
            p = ("먼저, " + p) if n == 0 else (("마지막 소식입니다. " if n == len(starts) - 1 else
                                              ("다음 소식입니다. ", "이어서 전해 드립니다. ")[n % 2]) + p)
        out.append(p)
    return "\n\n".join([intro] + out + [outro])


def verify_briefing(raw_script: str, cards: list, groups: list, now: dt.datetime, outro_fixed: str = "",
                    intro_fixed: str = ""):
    """대본 문장과 카드 문구를 함께 검증해 문제 문장을 뺌 → (고친 대본, 고친 카드, 뺀 목록, 검증 여부)
    문단의 첫 문장이 빠지거나 절반 이상이 빠지면 앞뒤가 끊겨 오해를 부를 수 있어 그 문단을 통째로 뺌"""
    intro, body, outro = _parse_script(raw_script, intro_fixed)
    if outro_fixed:                               # 맺음말은 항상 프로그램 문구
        outro = outro_fixed
    for c in cards or []:
        c["headline"], c["points"] = _fix_facts(c["headline"]), [_fix_facts(x) for x in c["points"]]
    body = [(k, _plain(_fix_today(p, groups[k - 1] if 1 <= k <= len(groups) else [], now),
                       _src_text(groups[k - 1]) if 1 <= k <= len(groups) else "")) for k, p in body]
    claims, where, i = [], {}, 0
    for pi, (k, p) in enumerate(body):
        for si, s_ in enumerate(_split_sents(p)):
            i += 1
            claims.append((i, k if 1 <= k <= len(groups) else 0, s_))
            where[i] = ("대본", pi, si)
    for ci, c in enumerate(cards or []):
        for li, t in enumerate([c["headline"]] + c["points"]):
            i += 1
            claims.append((i, ci + 1, t))
            where[i] = ("카드", ci, li)
    bad = verify_claims(claims, groups, now) if VERIFY_ON else {}
    if bad is None:
        return _transitions(intro, body, outro), [dict(c, points=c["points"][:3]) for c in cards or []], [], False
    text_of = {n: t for n, _, t in claims}
    n_script = sum(1 for w in where.values() if w[0] == "대본")
    n_card = len(where) - n_script
    f_script = sum(1 for n in bad if where.get(n, ("",))[0] == "대본")
    f_card = sum(1 for n in bad if where.get(n, ("",))[0] == "카드")
    if (f_script >= 2 and f_script > n_script * VERIFY_MAX_DROP) or (f_card >= 2 and f_card > n_card * VERIFY_MAX_DROP):
        # 너무 많이 걸렸으면 검증 쪽 오류일 가능성이 커서 빼지 않고 원고 그대로 + 의심 문장만 알림
        AUDIO_ERR.append(f"검증 과다(대본 {f_script}/{n_script}, 카드 {f_card}/{n_card}) → 적용 안 함")
        sus = [(("카드 " + str(where[n][1] + 1) + "번") if where[n][0] == "카드" else "대본", text_of[n], why)
               for n, why in sorted(bad.items()) if n in where]
        return _transitions(intro, body, outro), [dict(c, points=c["points"][:3]) for c in cards or []], sus, "over"
    removed, drop_s, drop_c = [], set(), set()
    for n, why in sorted(bad.items()):
        if n not in where:
            continue
        kind, a, b = where[n]
        (drop_s if kind == "대본" else drop_c).add((a, b))
        if kind == "카드":
            removed.append((f"카드 {a + 1}번", text_of[n], why))
    new_body, dead = [], set()
    for pi, (k, p) in enumerate(body):
        lead = pi == 0 or body[pi - 1][0] != k     # 이 사안의 첫 문단인지
        if k in dead and not lead:                 # 사안 첫 문단이 빠졌으면 이어지는 문단도 맥락이 없어 뺌
            removed.append((f"대본 사안 {k} 이어지는 문단", p, "같은 사안의 첫 문단이 빠져 함께 제외"))
            continue
        sents = _split_sents(p)
        gone = [si for si in range(len(sents)) if (pi, si) in drop_s]
        whys = [bad[n] for n in bad if where.get(n, ("", -1, -1))[:2] == ("대본", pi)]
        if gone and (0 in gone or len(gone) * 2 >= len(sents)):
            removed.append((f"대본 사안 {k} 문단 전체", p, "주요 문장이 원문과 달라 문단 전체 제외 — " + "; ".join(whys)[:120]))
            if lead:
                dead.add(k)
            continue
        for si in gone:
            removed.append((f"대본 사안 {k}", sents[si], bad[[n for n in bad if where.get(n) == ("대본", pi, si)][0]]))
        keep = [x for si, x in enumerate(sents) if si not in gone]
        if keep:
            new_body.append((k, " ".join(keep)))
    new_cards = []
    for ci, c in enumerate(cards or []):
        c = dict(c)
        if (ci, 0) in drop_c:                      # 제목이 틀렸으면 기사 제목 그대로
            c["headline"] = _card_fallback(groups[ci])["headline"]
        pts = [p for li, p in enumerate(c["points"], 1) if (ci, li) not in drop_c]
        c["points"] = pts[:3] or ["세부 내용은 기사 원문을 확인해 주세요."]
        new_cards.append(c)
    if removed:
        AUDIO_ERR.append(f"검증에서 {len(removed)}건 제외")
    return _transitions(intro, new_body, outro), new_cards, removed, True


def send_removed(removed: list, reply_to: int, verified: bool) -> None:
    if verified and not removed:
        return
    if verified == "over":
        text = ("⚠️ <b>자동 검증 결과 불확실</b> — 검증이 너무 많은 문장을 걸러 검증 쪽 오류일 수 있어 <b>빼지 않고 그대로</b> 보냈습니다. "
                "아래 의심 문장은 공유 전 원문과 확인해 주세요.\n" + "\n".join(
                    f"• [{html.escape(w)}] {html.escape(t)}\n  └ {html.escape(y)}" for w, t, y in removed))
    elif not verified:
        text = "⚠️ <b>자동 검증 실패</b> — 이번 브리핑·카드는 원문 대조를 거치지 않았습니다. 공유 전 내용을 확인해 주세요."
    else:
        text = "⚠️ <b>자동 검증에서 뺀 문장</b> (원문과 다르거나 근거가 없어 음성·카드에서 제외)\n" + "\n".join(
            f"• [{html.escape(w)}] {html.escape(t)}\n  └ {html.escape(y)}" for w, t, y in removed)
    payload = {"chat_id": TG_CHAT.strip(), "text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": True}
    if reply_to:
        payload["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
    r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", json=payload, timeout=30)
    if not r.ok:
        print(f"[검증 알림 오류] {r.status_code}: {r.text[:150]}", file=sys.stderr)


# ── 카드뉴스(브리핑마다 텔레그램 앨범, 부서 공유용·보고서풍) ─────────────────
# CARD=off 로 끄기. CARD_MAX: 사안 최대 개수(표지·목차·마무리 3장 + 사안 → 텔레그램 앨범 10장 이내라 7 이하)
CARD_ON = (os.getenv("CARD") or "on").strip().lower() != "off"
CARD_MAX = max(1, min(int(os.getenv("CARD_MAX") or "7"), 7))
CARD_STYLE = (os.getenv("CARD_STYLE") or "v2").strip().lower()   # v2: 정사각 새 틀 / v1: 예전 세로 틀
CARD_TAGS = {"조직·인력": "#2E5C8A", "인사": "#6B4F8A", "수사": "#8A3B3B", "국회·예산": "#3D6B4F",
             "제도·협의": "#7A6230", "기타": "#5B6B7F"}
CARD_DISCLAIMER = "언론보도 요약 · 사실관계 확인 전 · 내부 참고용"
_FONT_URLS = ["https://raw.githubusercontent.com/orioncactus/pretendard/v1.3.9/packages/pretendard/dist/web/variable/woff2/PretendardVariable.woff2",
              "https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/variable/woff2/PretendardVariable.woff2"]
_ORG_RE = re.compile(r"[가-힣]{1,8}(?:중수청|공소청|지검|지청|경찰청|지법|고법)")


def _num_norm(t: str) -> str:
    return re.sub(r"(?<=\d),(?=\d{3})", "", t)


def unsupported(text: str, src: str, now: dt.datetime = None) -> list:
    """글에 나온 기관명·숫자 중 원래 제목·요약에 없는 것(AI가 지어냈거나 잘못 바꾼 것)"""
    src_n = _num_norm(src)
    bad = [o for o in set(_ORG_RE.findall(text)) if o not in src]
    nums = set(re.findall(r"\d+(?:\.\d+)?", src_n))
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*(월|년)?", _num_norm(text)):
        n, unit = m.group(1), m.group(2)
        if n in nums or (unit == "월" and n.isdigit() and 1 <= int(n) <= 12) or (unit == "년" and n.startswith("20")):
            continue
        if now and n in (str(now.year), str(now.month), str(now.day)):
            continue
        bad.append(n)
    return sorted(set(bad))


def _src_text(g: list) -> str:
    # 카드 AI에 본문 발췌도 주므로, 대조 기준에도 본문을 넣어야 본문에 있는 숫자·기관명을 '지어낸 것'으로 오인하지 않음
    return "\n".join(f"{x.get('title', '')} {x.get('summary') or ''} {(x.get('body') or '')[:2500]}" for x in g)


_HANJA = {"李": "이", "尹": "윤", "文": "문", "朴": "박", "與": "여당", "野": "야당", "檢": "검찰", "警": "경찰",
          "靑": "청와대", "軍": "군", "美": "미국", "中": "중국", "日": "일본", "北": "북한", "韓": "한국", "英": "영국",
          "故": "고", "前": "전", "現": "현", "新": "신", "反": "반", "親": "친", "對": "대"}


def no_hanja(t: str) -> str:
    """카드 글꼴(Pretendard)에 한자가 없어 네모(▤)로 깨지므로 흔한 한자 약칭을 한글로, 나머지 한자는 지움"""
    t = re.sub(r"[\u4e00-\u9fff]", lambda m: _HANJA.get(m.group(0), ""), t)
    return re.sub(r"\s{2,}", " ", t).strip()


def _cut(t: str, lim: int) -> str:
    return t if len(t) <= lim else (t[:lim].rsplit(" ", 1)[0] if " " in t[:lim] else t[:lim]) + "…"


def _card_fallback(g: list) -> dict:
    """AI 문구를 못 쓸 때: 기사 제목과 요약 문장을 그대로(지어낸 말 없이)"""
    a = g[0]
    head = re.sub(r"\s+", " ", re.sub(r"\[[^\]]*\]|【[^】]*】", " ", a["title"])).strip()
    sents = [x.strip() for x in re.split(r"(?<=[.!?])\s+", tidy_summary(a.get("summary") or "")) if len(x.strip()) > 4]
    sents = [re.sub(r"\s*\((?:제목 기준|사진 기사)\)", "", x) for x in sents if not _META_PT.fullmatch(x)]
    pts = [_cut(x, 70) for x in sents[:3]] or ["기사 원문을 확인해 주세요."]
    return {"tag": "기타", "headline": _cut(head, 46), "points": pts}


_META_PT = re.compile(r"본문\s*(?:정보|내용)?\s*(?:이\s*)?없|사진 기사|포토 기사|제목 기준|원문을? 확인")


def card_copy(groups: list, now: dt.datetime) -> list:
    """사안마다 카드 문구 {tag, headline, points}. AI가 쓰고, 원문에 없는 기관명·숫자가 섞이면 한 번 고쳐 쓰게 하고
    그래도 남으면 그 사안만 요약 원문으로 대체"""
    out = [None] * len(groups)
    if ANTHROPIC_KEY:
        items = []
        for n, g in enumerate(groups):
            sums = []
            for x in g[:4]:
                if x["summary"] and x["summary"] not in sums:
                    sums.append(x["summary"])
            items.append(f"[{n}] 제목: {g[0]['title']}\n요약:\n" + "\n".join(sums or ["(요약 없음)"]) + "\n" + _excerpts(g))
        base = (
            "아래 사안들로 부서 내부 공유용 카드뉴스 문구를 만든다. 사안마다 카드 한 장이다.\n"
            "[형식] JSON 배열만 출력: [{\"i\":0,\"tag\":\"수사\",\"headline\":\"...\",\"points\":[\"...\",\"...\",\"...\"]}]\n"
            f"- tag는 다음 중 하나: {', '.join(CARD_TAGS)}\n"
            "- headline: 무슨 일인지 한 줄, 공백 포함 28자 이내, 명사형으로 끝냄(예: '서울중수청, ○○ 사건 공수처 이첩').\n"
            "- points: 핵심 사실을 중요한 순서대로 4개까지(3개 이상을 원칙으로, 기사에 사실이 부족할 때만 2개, 정말 하나뿐일 때만 1개), "
            "각각 공백 포함 40자 이내, 개조식(~함/~임 대신 명사형·'~하기로' 등으로 짧게). 제목을 되풀이하거나 '본문 없음', '사진 기사' 같은 설명은 쓰지 않는다.\n"
            "[금지]\n"
            "- 요약에 없는 사실·숫자·기관명·직함을 쓰지 않는다. 기관명은 요약·본문에 적힌 그대로 쓴다(예: '서울중수청'을 '서울중앙지검'으로, '서울남부'를 '서울남부지방공소청'으로 바꾸지 않음).\n"
            "- 숫자는 요약에 있는 값 그대로 쓴다(단위 바꾸기·반올림 금지).\n"
            "- 평가·전망·의견 표현('논란 확산', '파장', '비상', '난항 불가피' 등 요약에 없는 해석)을 쓰지 않는다.\n"
            "- 언론사 이름은 쓰지 않는다(출처는 따로 붙임). 한자 약칭(李·尹·與·野·檢 등)은 한글로 풀어 쓴다(야당, 검찰 등).\n"
            f"- 오늘은 {now:%Y-%m-%d}. 기본 사실:\n{FACTS}\n- '신설될·새로 생기는' 같은 미래 표현 금지. 요약과 본문이 다르면 본문을 따른다.\n"
            "- 요약에 없는 인과·시간 관계('~에 따라', '같은 날')를 만들지 않는다.\n\n" + "\n\n".join(items))
        prompt = base
        for attempt in range(2):
            text = ask_claude(prompt, 4000)
            try:
                data, _ = json.JSONDecoder().raw_decode(text[text.index("["):])
            except Exception as e:
                AUDIO_ERR.append(f"카드 문구: {type(e).__name__}: {str(e)[:150]}")
                break
            notes = []
            for d in data:
                try:
                    i = int(d["i"])
                    pts = [str(p).strip() for p in d.get("points", []) if str(p).strip() and not _META_PT.search(str(p))][:4]
                    c = {"tag": d.get("tag") if d.get("tag") in CARD_TAGS else "기타",
                         "headline": str(d["headline"]).strip(), "points": pts}
                except Exception:
                    continue
                if not (0 <= i < len(groups)) or not c["headline"] or not pts:
                    continue
                bad = unsupported(c["headline"] + " " + " ".join(pts), _src_text(groups[i]), now)
                if bad:
                    notes.append(f"[{i}] 요약에 없는 표현: {', '.join(bad)}")
                    AUDIO_ERR.append(f"카드 {i} 원문 불일치: {', '.join(bad)}")
                else:
                    out[i] = c
            if not notes:
                break
            prompt = base + "\n\n[고쳐 쓰기] 앞서 쓴 문구에 요약에 없는 기관명·숫자가 있었다. 요약 그대로 다시 써라.\n" + "\n".join(notes)
    return [c or _card_fallback(g) for c, g in zip(out, groups)]


def _card_font_css() -> str:
    path = os.path.join(os.getenv("RUNNER_TEMP") or "/tmp", "PretendardVariable.woff2")
    if not os.path.exists(path):
        for u in _FONT_URLS:
            try:
                r = requests.get(u, timeout=30)
                if r.ok and len(r.content) > 100000:
                    open(path, "wb").write(r.content)
                    break
            except Exception:
                pass
    if os.path.exists(path):
        import base64
        b64 = base64.b64encode(open(path, "rb").read()).decode()
        return ("@font-face{font-family:'Pretendard';font-weight:45 920;"
                f"src:url(data:font/woff2;base64,{b64}) format('woff2')}}")
    if not subprocess.run(["fc-list", ":lang=ko"], capture_output=True, text=True).stdout.strip():
        raise RuntimeError("한글 글꼴 없음(글꼴 내려받기 실패)")
    return ""


_CARD_CSS = """
*{margin:0;padding:0;box-sizing:border-box}
html,body{width:1080px;height:1350px}
body{background:#fff;color:#1B2A3D;font-family:'Pretendard','Noto Sans CJK KR','Noto Sans KR',sans-serif;
 word-break:keep-all;overflow-wrap:break-word;position:relative;overflow:hidden;letter-spacing:-0.5px}
.foot{position:absolute;left:80px;right:80px;bottom:48px;border-top:2px solid #D9E0E8;padding-top:22px;
 font-size:23px;color:#8592A3;display:flex;justify-content:space-between;gap:24px}
.ic{display:flex;align-items:center;justify-content:center;flex:none}
"""
_NAVY, _NAVY2, _SOFT, _LINE, _SUB = "#003764", "#2E5C8A", "#EEF3F9", "#D9E0E8", "#5B6B7F"
# 분류별 선 아이콘(직접 그린 단순 도형 — 저작권 문제 없음)
_ICON = {
    "수사": '<circle cx="26" cy="26" r="15"/><line x1="37" y1="37" x2="52" y2="52"/>',
    "인사": '<circle cx="32" cy="20" r="10"/><path d="M12 54c2-12 10-18 20-18s18 6 20 18"/>',
    "국회·예산": '<path d="M8 22 32 8l24 14"/><line x1="8" y1="54" x2="56" y2="54"/><line x1="14" y1="26" x2="14" y2="48"/>'
                '<line x1="26" y1="26" x2="26" y2="48"/><line x1="38" y1="26" x2="38" y2="48"/><line x1="50" y1="26" x2="50" y2="48"/>',
    "제도·협의": '<rect x="12" y="6" width="40" height="52" rx="4"/><line x1="20" y1="20" x2="44" y2="20"/>'
                '<line x1="20" y1="30" x2="44" y2="30"/><line x1="20" y1="40" x2="36" y2="40"/>',
    "조직·인력": '<rect x="24" y="6" width="16" height="12" rx="2"/><rect x="6" y="42" width="16" height="12" rx="2"/>'
                '<rect x="42" y="42" width="16" height="12" rx="2"/><path d="M32 18v12M14 42V30h36v12"/>',
    "기타": '<rect x="8" y="10" width="48" height="44" rx="4"/><line x1="16" y1="22" x2="48" y2="22"/>'
           '<line x1="16" y1="32" x2="48" y2="32"/><line x1="16" y1="42" x2="38" y2="42"/>',
    "링크": '<path d="M27 37a10 10 0 0 0 14 0l9-9a10 10 0 0 0-14-14l-4 4"/><path d="M37 27a10 10 0 0 0-14 0l-9 9a10 10 0 0 0 14 14l4-4"/>',
}


def _icon(tag: str, size: int, color: str, sw: float = 4) -> str:
    return (f"<svg width='{size}' height='{size}' viewBox='0 0 64 64' fill='none' stroke='{color}' stroke-width='{sw}' "
            f"stroke-linecap='round' stroke-linejoin='round'>{_ICON.get(tag, _ICON['기타'])}</svg>")


def _key_figure(headline: str, points: list):
    """검증된 요점 문장에 이미 있는 숫자만 사용(새로 계산하거나 만들지 않음).
    'N건 중 M건'처럼 비율이 문장에 그대로 있을 때만 막대로 보여 줌(맥락 없는 숫자는 생략)."""
    for p in points:
        m = re.search(r"(\d[\d,]*)\s*건\s*중\s*(?:[^\d]{0,12})?(\d[\d,]*)\s*건", p)
        if m:
            a, b = int(m.group(1).replace(",", "")), int(m.group(2).replace(",", ""))
            if a and b <= a:
                return {"big": m.group(2), "unit": "건", "sub": f"전체 {m.group(1)}건 중", "ratio": b / a}
    return None


def _esc(t) -> str:
    return html.escape(str(t))


def _fit(text: str, sizes: list) -> int:
    """글이 길면 글자를 한 단계씩 줄임: sizes=[(최대 글자수, 크기), ...]"""
    for lim, size in sizes:
        if len(text) <= lim:
            return size
    return sizes[-1][1]


def _mi(h: int) -> str:
    """중수청 기관문양(MI) 원본 — 형태·색상·비율 변경 금지 규정에 따라 흰 바탕 위에 원본 그대로, 높이만 맞춤"""
    fp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "sci_mi.png")
    if not os.path.exists(fp):
        return ""
    import base64
    return f"<img src='data:image/png;base64,{base64.b64encode(open(fp, 'rb').read()).decode()}' style='height:{h}px;width:auto;display:block'>"


def _plate(h: int, pad: str) -> str:
    m = _mi(h)
    return f"<div style='display:inline-block;background:#fff;border-radius:16px;padding:{pad}'>{m}</div>" if m else ""


def _deco() -> str:
    return ("<div style='position:absolute;right:-70px;top:-60px;width:440px;height:440px;border-radius:50%;background:rgba(255,255,255,.06)'></div>"
            "<div style='position:absolute;right:110px;top:170px;width:220px;height:220px;border-radius:50%;background:rgba(79,134,198,.22)'></div>")


def card_pages(cards: list, sources: list, when: str, when_short: str, label: str, n_art: int, more: int) -> list:
    cards = [dict(c, headline=no_hanja(c["headline"]), points=[no_hanja(p) for p in c["points"]][:3]) for c in cards]
    total = len(cards) + 2
    wrap = lambda body: f"<!DOCTYPE html><html><head><meta charset='utf-8'><style>{_CARD_CSS}</style></head><body>{body}</body></html>"
    col = lambda t: CARD_TAGS.get(t, _SUB)
    k = len(cards)
    # ── 표지: 남색 머리 + 사안 타일(아이콘·분류·제목) ──
    ts, ti, tg = (34, 70, 20) if k <= 4 else (31, 60, 16) if k <= 6 else (26, 48, 10)
    tiles = "".join(
        f"<div style='flex:1;max-height:{190 if k <= 4 else 150}px;display:flex;gap:24px;align-items:center;padding:8px 28px;background:#fff;border-radius:20px;border:2px solid {_LINE}'>"
        f"<div class='ic' style='width:{ti}px;height:{ti}px;border-radius:16px;background:{col(c['tag'])}'>{_icon(c['tag'], int(ti * .6), '#fff')}</div>"
        f"<div><div style='font-size:{ts - 9}px;font-weight:700;color:{col(c['tag'])}'>{i:02d} · {_esc(c['tag'])}</div>"
        f"<div style='font-size:{ts}px;font-weight:700;line-height:1.3;margin-top:2px'>{_esc(c['headline'])}</div></div></div>"
        for i, c in enumerate(cards, 1))
    big = 84 if k <= 4 else 72
    pages = [wrap(
        f"<div style='background:{_NAVY};color:#fff;padding:44px 80px {80 if k <= 4 else 64}px;position:relative;overflow:hidden'>{_deco()}"
        f"<div style='display:flex;align-items:center;gap:20px;font-size:28px;font-weight:700;position:relative'>"
        f"{_plate(40, '12px 20px')}"
        f"<div style='padding:10px 22px;border:2px solid rgba(255,255,255,.5);border-radius:999px;font-weight:500'>{_esc(when)}</div>"
        f"<div style='margin-left:auto;opacity:.85'>1 / {total}</div></div>"
        f"<div style='font-size:{big}px;font-weight:900;line-height:1.15;margin-top:{56 if k <= 4 else 40}px;position:relative'>언론보도<br>{_esc(label)}</div></div>"
        f"<div style='position:absolute;left:0;right:0;bottom:0;top:300px;background:{_SOFT};z-index:-1'></div>"
        f"<div style='position:absolute;top:{480 if k <= 4 else 420}px;bottom:150px;left:60px;right:60px;display:flex;flex-direction:column;gap:{tg}px'>{tiles}</div>"
        f"<div class='foot'><span>{CARD_DISCLAIMER}</span><span>카드 {total}장</span></div>")]
    # ── 사안 카드: 머리띠 + 제목 + (핵심 수치) + 요점 상자(연한 바탕을 고르게 채움) ──
    for i, (c, src) in enumerate(zip(cards, sources), 1):
        cc, pts = col(c["tag"]), c["points"]
        kf = _key_figure(c["headline"], pts)
        ps = _fit(" ".join(pts), [(70, 42), (110, 39), (150, 35), (999, 32)])
        hs = _fit(c["headline"], [(22, 62), (32, 56), (999, 48)])
        kbox = ""
        if kf:
            bar = (f"<div style='height:18px;background:#D6E2F0;border-radius:9px;margin-top:14px;overflow:hidden'>"
                   f"<div style='height:100%;width:{max(kf['ratio'], .02) * 100:.1f}%;background:{cc};border-radius:9px'></div></div>"
                   if kf["ratio"] is not None else "")
            kbox = (f"<div style='display:flex;gap:34px;align-items:center;margin-top:34px;padding:26px 36px;border:3px solid {_SOFT};border-radius:24px'>"
                    f"<div style='white-space:nowrap;color:{cc}'><b style='font-size:84px;font-weight:900;line-height:1'>{_esc(kf['big'])}</b>"
                    f"<span style='font-size:34px;font-weight:800;margin-left:4px'>{_esc(kf['unit'])}</span></div>"
                    f"<div style='flex:1'><div style='font-size:26px;color:{_SUB};font-weight:600'>핵심 수치"
                    f"{(' · ' + _esc(kf['sub'])) if kf['sub'] else ''}</div>{bar}</div></div>")
        rows = "".join(
            f"<div style='display:flex;gap:24px;align-items:flex-start;background:#fff;border-radius:22px;padding:30px 34px;"
            f"box-shadow:0 3px 0 {_LINE}'>"
            f"<div class='ic' style='width:50px;height:50px;border-radius:50%;background:{cc};color:#fff;font-size:26px;font-weight:800;"
            f"margin-top:{max(ps * .75 - 25, 0):.0f}px'>{n}</div>"
            f"<div style='font-size:{ps}px;line-height:1.5;font-weight:600'>{_esc(p)}</div></div>"
            for n, p in enumerate(pts, 1))
        pages.append(wrap(
            f"<div style='height:1350px;display:flex;flex-direction:column'>"
            f"<div style='background:{_NAVY};color:#fff;padding:40px 80px 36px;display:flex;align-items:center;gap:26px'>"
            f"<div class='ic' style='width:80px;height:80px;border-radius:22px;background:rgba(255,255,255,.12)'>{_icon(c['tag'], 50, '#fff')}</div>"
            f"<div style='flex:1'><div style='font-size:25px;opacity:.75'>중수청 언론보도 · {_esc(when_short)}</div>"
            f"<div style='font-size:34px;font-weight:800;margin-top:4px'>{i:02d} &nbsp;{_esc(c['tag'])}</div></div>"
            f"<div style='font-size:28px;font-weight:700;opacity:.85'>{i + 1} / {total}</div></div>"
            f"<div style='height:10px;background:{cc}'></div>"
            f"<div style='padding:50px 80px 44px'><div style='font-size:{hs}px;font-weight:900;line-height:1.28'>{_esc(c['headline'])}</div>{kbox}</div>"
            f"<div style='flex:1;background:{_SOFT};position:relative;overflow:hidden;padding:44px 80px 150px'>"
            f"<div style='position:absolute;right:-50px;bottom:90px;opacity:.07'>{_icon(c['tag'], 460, _NAVY, 3)}</div>"
            f"<div style='position:relative;height:100%;display:flex;flex-direction:column;justify-content:flex-start;gap:28px'>{rows}</div></div></div>"
            f"<div class='foot'><span>출처: {_esc(src)}</span><span>{CARD_DISCLAIMER}</span></div>"))
    # ── 마무리 ──
    extra = f"이 밖에 중수청 관련 사안 {more}개는<br>텔레그램 알림에서 확인하세요.<br><br>" if more else ""
    pages.append(wrap(
        f"<div style='background:{_NAVY};color:#fff;height:1350px;padding:0 80px;display:flex;flex-direction:column;justify-content:center;position:relative;overflow:hidden'>{_deco()}"
        f"<div style='position:absolute;top:56px;left:80px;right:80px;display:flex;justify-content:space-between;font-size:28px;font-weight:700;opacity:.85'>"
        f"<span>언론보도 모니터링</span><span>{total} / {total}</span></div>"
        f"<div class='ic' style='width:120px;height:120px;border-radius:30px;background:rgba(255,255,255,.12);position:relative'>{_icon('링크', 72, '#fff')}</div>"
        f"<div style='font-size:60px;font-weight:900;line-height:1.35;margin-top:44px;position:relative'>기사 원문은<br>텔레그램 메시지의<br>"
        f"<span style='color:#9CC3EA'>링크</span>에서 확인하세요</div>"
        f"<div style='font-size:31px;line-height:1.7;margin-top:52px;opacity:.85;position:relative;border-top:2px solid rgba(255,255,255,.25);padding-top:40px'>{extra}"
        f"이 카드는 언론 보도 내용을 요약한 것으로<br>사실관계가 확인되지 않은 내용이 포함될 수 있습니다.<br>"
        f"외부 배포를 금합니다.</div>"
        f"<div style='position:absolute;left:0;right:0;bottom:64px;display:flex;justify-content:center'>{_plate(56, '18px 30px')}</div></div>"))
    return pages


def _chrome() -> str:
    for c in [os.getenv("CHROME_PATH") or "", "google-chrome", "google-chrome-stable", "chromium", "chromium-browser"]:
        p = shutil.which(c) if c else None
        if p:
            return p
    return ""


def render_cards(pages: list, folder: str, height: int = 1350) -> list:
    chrome = _chrome()
    if not chrome:
        raise RuntimeError("크롬 없음")
    font = _card_font_css()
    os.makedirs(folder, exist_ok=True)
    paths = []
    for n, h in enumerate(pages, 1):
        hp, png = os.path.join(folder, f"card_{n}.html"), os.path.join(folder, f"card_{n}.png")
        open(hp, "w", encoding="utf-8").write(h.replace("<style>", "<style>" + font, 1))
        subprocess.run([chrome, "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
                        "--force-device-scale-factor=1", f"--window-size=1080,{height}", "--virtual-time-budget=4000",
                        f"--screenshot={png}", "file://" + os.path.abspath(hp)],
                       capture_output=True, timeout=90)
        if not os.path.exists(png) or os.path.getsize(png) < 5000:
            raise RuntimeError(f"카드 {n} 이미지 생성 실패")
        paths.append(png)
    return paths


def send_cards(paths: list, caption: str, reply_to: int = 0) -> None:
    media = [{"type": "photo", "media": f"attach://c{i}"} for i in range(len(paths))]
    media[0].update({"caption": caption, "parse_mode": "HTML"})
    data = {"chat_id": TG_CHAT.strip()}
    if reply_to:
        data["reply_parameters"] = json.dumps({"message_id": reply_to, "allow_sending_without_reply": True})
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMediaGroup"
    for _ in range(2):
        fs = [open(p, "rb") for p in paths]
        try:
            r = requests.post(url, data={**data, "media": json.dumps(media, ensure_ascii=False)},
                              files={f"c{i}": (os.path.basename(p), f, "image/png") for i, (p, f) in enumerate(zip(paths, fs))},
                              timeout=180)
        finally:
            for f in fs:
                f.close()
        if r.ok:
            return
        print(f"[카드 발송 오류] {r.status_code}: {r.text[:200]}", file=sys.stderr)
        media[0].pop("parse_mode", None)           # 서식 문제일 수 있어 서식 없이 다시
        media[0]["caption"] = html.unescape(re.sub(r"<[^>]+>", "", caption))
    r.raise_for_status()


def refill_bodies(use: list) -> tuple:
    """보내기 전 점검 ①: 본문 없이 제목만 있는 사안은 본문을 한 번 더 찾음(페이지 데이터·네이버 뉴스 사본).
    → (본문 있는 기사 수, 전체 기사 수, 끝내 제목뿐인 사안 번호 목록)"""
    title_only = []
    for n, g in enumerate(use, 1):
        if not any((x.get("body") or "").strip() for x in g):
            for x in g[:3]:
                try:
                    b = fetch_body(x)
                except Exception:
                    b = ""
                if b:
                    x["body"] = b
                    break
        if not any((x.get("body") or "").strip() for x in g):
            title_only.append(n)
            for x in g:
                x["_title_only"] = True
    nb = sum(1 for g in use for x in g if (x.get("body") or "").strip())
    return nb, sum(len(g) for g in use), title_only


def thicken_cards(cards: list, groups: list) -> None:
    """보내기 전 점검 ②: 요점이 1개뿐인 카드는 같은 사안 다른 기사들의 요약 문장으로 보충(뒤이은 원문 검증을 거침)"""
    for c, g in zip(cards, groups):
        if len(c["points"]) >= 2:
            continue
        have = [c["headline"]] + c["points"]
        for x in g:
            for sent in re.split(r"(?<=[.!?])\s+|(?<=함)\s+|(?<=임)\s+|(?<=됨)\s+", tidy_summary(x.get("summary") or "")):
                sent = re.sub(r"\s*\((?:제목 기준|사진 기사)\)", "", sent).strip(" .")
                if len(sent) < 8 or _META_PT.search(sent) or any(similar(sent, h, 0.5) for h in have):
                    continue
                c["points"].append(_cut(sent, 44))
                have.append(sent)
                FIXES.append("카드 보충")
                if len(c["points"]) >= 3:
                    break
            if len(c["points"]) >= 3:
                break


def card_briefing(use: list, label: str, now: dt.datetime, n_art: int, more: int, reply_to: int,
                  cards: list = None, warn: str = "", since: dt.datetime = None, until: dt.datetime = None) -> None:
    show = use[:CARD_MAX]
    more += len(use) - len(show)
    cards = cards if cards is not None else card_copy(show, now)
    sources = [press_name(g[0]["press"]) + (f" 외 {len(g) - 1}건" if len(g) > 1 else "")
               + (" · 본문 미확인(제목 기준)" if any(x.get("_title_only") for x in g) else "") for g in show]
    wd = "월화수목금토일"[now.weekday()]
    folder = os.path.join(os.getenv("RUNNER_TEMP") or ".", f"cards_{now:%m%d_%H%M}")
    if CARD_STYLE == "v2":                         # 정사각: 표지·목차·사안(시각화 포함 사안당 최대 2장)·마무리
        import cards_v2
        pages = cards_v2.build(cards, sources, now, label, since or now - dt.timedelta(hours=14), until or now)
        paths = render_cards(pages, folder, 1080)
    else:
        pages = card_pages(cards, sources, f"{now:%Y. %m. %d.}({wd}) {now:%H:%M}", f"{now:%m.%d.}({wd}) {label}",
                           f"{label} 브리핑", n_art, more)
        paths = render_cards(pages, folder)
    caption = (warn + f"🗂 <b>중수청 {label} 카드뉴스</b> (부서 공유용) · 사안 {len(cards)}개 · {len(paths)}장\n"
               "사안별 기사 링크는 아래 메시지에 모두 있습니다.")
    for k in range(0, len(paths), 10):             # 텔레그램 앨범은 한 번에 10장까지
        send_cards(paths[k:k + 10], caption if k == 0 else f"🗂 {label} 카드뉴스 (이어서)", reply_to)
    # 설명란은 1,024자 제한이라 링크가 잘렸음 → 모든 기사 링크를 따로 보냄(4,096자씩 나눔)
    e = html.escape
    blocks = []
    for i, (c, g) in enumerate(zip(cards, show), 1):
        seen_u, links = set(), []
        for x in g:
            if x["url"] in seen_u:
                continue
            seen_u.add(x["url"])
            links.append(f"<a href=\"{e(x['url'], quote=True)}\">{e(press_name(x['press']))}</a>")
        blocks.append(f"<b>{i}. {e(c['headline'])}</b>\n   " + " · ".join(links))
    msgs, cur = [], f"🔗 <b>카드뉴스 기사 링크</b> ({label})\n\n"
    for b in blocks:
        if len(cur) + len(b) > 3900:
            msgs.append(cur)
            cur = ""
        cur += b + "\n\n"
    msgs.append(cur)
    for m in msgs:
        payload = {"chat_id": TG_CHAT.strip(), "text": m.strip(), "parse_mode": "HTML",
                   "disable_web_page_preview": True, "disable_notification": True}
        if reply_to:
            payload["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", json=payload, timeout=30)
        if not r.ok:
            print(f"[카드 링크 발송 오류] {r.status_code}: {r.text[:150]}", file=sys.stderr)
    print(f"카드뉴스({label}) 발송: {len(paths)}장, 링크 {sum(len(g) for g in show)}건")


def card_fields(c: dict) -> dict:
    """원고 파일의 카드 한 개 → 카드 틀에 넘길 값(마지막 기계 교정 포함)"""
    out = {"tag": c.get("tag") if c.get("tag") in CARD_TAGS else "기타",
           "headline": _fix_facts(c["headline"]), "points": [_fix_facts(x) for x in c.get("points", [])][:3]}
    if c.get("lead"):
        out["lead"] = _fix_facts(c["lead"])
    if (c.get("stat") or {}).get("big"):
        out["stat"] = c["stat"]
    if (c.get("viz") or {}).get("type"):
        out["viz"] = c["viz"]
    return out


def manual_briefing(state: dict, now: dt.datetime, path: str) -> None:
    """Claude가 쓴 원고 파일로 음성·카드 발송(AI 호출 없음).
    파일: {label, cut, paras:[{text, sources:[기사 id]}], cards:[{tag, headline, points, sources}], articles:{id:{title,press,url}}, more, notes}"""
    d = json.load(open(path, encoding="utf-8"))
    label = d["label"]
    arts = d["articles"]
    by_url = {r["url"]: r for r in state.get("audio_log", [])}
    groups, body = [], []
    for k, p_ in enumerate(d["paras"], 1):
        g = [dict(arts[i], body=(by_url.get(arts[i]["url"]) or {}).get("body", "")) for i in p_.get("sources", []) if i in arts]
        groups.append(g)
        text = _plain(_fix_facts(p_["text"].strip()), _src_text(g))     # 마지막 기계 교정(관할 표현·평가 표현 등)
        body.append((k, text))
    raw = _transitions(intro_text(label, now), body, closing_text(label, now, int(d.get("more") or 0)))
    script, spoken = speakable(raw), speakable(raw, sound=True)
    wd = "월화수목금토일"
    since = dt.datetime.fromisoformat(d.get("since") or state.get("audio_cut") or (now - dt.timedelta(hours=14)).isoformat())
    span = (f"{since:%H:%M}~{now:%H:%M}" if since.date() == now.date() else
            f"{since:%m.%d.}({wd[since.weekday()]}) {since:%H:%M} ~ {now:%m.%d.}({wd[now.weekday()]}) {now:%H:%M}")
    n_art = len({a["url"] for a in arts.values()})
    lines = [f"{n}. {html.escape(g[0]['title'][:60])} ({html.escape(press_name(g[0]['press']))})" + (f" 외 {len(g) - 1}건" if len(g) > 1 else "")
             for n, g in enumerate(groups, 1) if g]
    test = bool(d.get("test"))
    caption = ("🧪 <b>시험 · Claude 작성</b>\n" if test else "") + f"🎧 <b>중수청 {label} 브리핑</b>\n{span} 중수청 기사 {n_art}건 · {len(groups)}개 사안\n" + "\n".join(lines)
    if d.get("title_only"):
        caption += "\n📎 본문 미확인으로 원고 제외: " + ", ".join(html.escape(t[:30]) for t in d["title_only"])
    caption += "\n🔎 " + html.escape(d.get("check") or "Claude 작성 · 원문 대조 완료")
    if len(caption) > 1000:
        caption = caption[:990].rsplit("\n", 1)[0] + "\n…"
    state["audio_scripts"] = (state.get("audio_scripts", []) + [[now.isoformat(), label, script, "Claude 작성"]])[-10:]
    fpath = os.path.join(os.getenv("RUNNER_TEMP") or ".", f"중수청브리핑_{now:%m%d_%H%M}.mp3")
    engine = tts_mp3(spoken, fpath)
    mixed = add_bgm(fpath, now)
    mid = send_audio(mixed, caption, f"{now:%m.%d.} {label} 중수청 브리핑", d.get("audio_format", ""))
    if (os.getenv("AUDIO_SCRIPT_MSG") or "on").strip().lower() != "off" and not d.get("audio_only"):
        send_script(script, mid, "📝 <b>음성 원고</b> (눌러서 펼치기 · 문단 번호로 지적해 주세요)\n")
    print(f"Claude 원고 음성 발송({label}): 원고 {len(script)}자, {engine}, 형식 {d.get('audio_format') or AUDIO_FORMAT}")
    if CARD_ON and d.get("cards") and not d.get("audio_only"):
        use, cards = [], []
        for c in d["cards"][:CARD_MAX]:
            g = [arts[i] for i in c.get("sources", []) if i in arts]
            if not g:
                continue
            use.append(g)
            cards.append(card_fields(c))
        until = dt.datetime.fromisoformat(d["cut"]) if d.get("cut") else now
        card_briefing(use, label, now, n_art, 0, mid, cards, "", since, min(until, now))
    if test:                                       # 시험은 발송 범위·시각 기록을 바꾸지 않음
        return
    slot = None                                    # 이 시간대 자동 브리핑(대비책)이 다시 나가지 않게
    for t in AUDIO_TIMES.split(","):
        m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", t)
        if m and (int(m.group(1)) < 12) == (label == "아침"):
            slot = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
    state["last_audio"] = max(now, slot).isoformat() if slot else now.isoformat()
    if d.get("cut"):
        state["audio_cut"] = d["cut"]


def audio_briefing(state: dict, now: dt.datetime) -> None:
    label = audio_due(state, now)
    if not label:
        return
    since = dt.datetime.fromisoformat(state["audio_cut"]) if state.get("audio_cut") else now - dt.timedelta(hours=14)
    if label == "시험":                            # 시험은 언제 눌러도 들어 볼 수 있게 최근 24시간 기사로
        since = now - dt.timedelta(hours=24)
    recs = [from_record(r) for r in state.get("audio_log", []) if dt.datetime.fromisoformat(r["found"]) > since]
    recs = [r for r in recs if not is_photo(r)]    # 사진 기사는 음성·카드에서 제외(텍스트 알림에는 그대로)
    for r in recs:                                 # 실행마다 AI 묶음 번호가 달라서 실행 시각과 함께 구분
        r["group"] = f"{r['found']}|{r['group']}" if r.get("group") is not None else None
    wd = "월화수목금토일"
    span = (f"{since:%H:%M}~{now:%H:%M}" if since.date() == now.date() else
            f"{since:%m.%d.}({wd[since.weekday()]}) {since:%H:%M} ~ {now:%m.%d.}({wd[now.weekday()]}) {now:%H:%M}")
    head = f"🎧 <b>중수청 {label} 브리핑</b>\n{span} 중수청 기사 "
    if not recs:
        send_telegram(head + "없음")
        print(f"음성 브리핑({label}): 새 중수청 기사 없음")
    else:
        groups = group_articles(sorted(recs, key=lambda x: x["found"]))
        # 실행마다 따로 들어온 같은 사안(예: 9시·11시·15시에 각각 들어온 같은 사건 기사)을 하나로 합침
        merged = [[a for i in c for a in groups[i]] for c in audio_cluster(groups)]
        merged.sort(key=lambda g: -len(g))   # AI가 준 순서와 무관하게 기사가 많은 사안부터(파이썬 정렬은 안정적이라 동수면 AI 순서 유지)
        use, more = merged[:AUDIO_MAX], max(len(merged) - AUDIO_MAX, 0)
        FIXES.clear()
        n_body, n_all, title_only = refill_bodies(use)
        lab = "오후" if label == "시험" and now.hour >= 12 else ("아침" if label == "시험" else label)
        v2 = None
        if BRIEF_MODE in ("new", "both") and ANTHROPIC_KEY:
            try:
                v2 = brief_v2(use, label, now, more)
            except Exception as e:
                AUDIO_ERR.append(f"새 방식: {type(e).__name__}: {str(e)[:150]}")
                print(f"[새 방식 실패] {type(e).__name__}: {e}", file=sys.stderr)
                v2 = {"script": "", "title_only": [], "log": list(V2_LOG) + [f"새 방식 실패: {type(e).__name__}: {str(e)[:150]}"],
                      "n_facts": 0}
        if BRIEF_MODE == "new" and v2 and v2.get("script"):
            raw_script, cards, removed, verified = v2["raw"], v2["cards"], v2["removed"], v2["verified"]
            title_only = v2["title_only"]
            dropped_use = [use[n - 1] for n in title_only]
            use = v2["groups"]                    # 원고·카드에 실제로 들어간 사안만(제목뿐인 사안은 따로 목록)
        else:
            dropped_use = []
            raw_script = audio_script(use, lab, since, now, more)
            try:
                cards = card_copy(use[:CARD_MAX], now) if CARD_ON else []
            except Exception as e:
                AUDIO_ERR.append(f"카드 문구: {type(e).__name__}: {str(e)[:150]}")
                cards = [_card_fallback(g) for g in use[:CARD_MAX]] if CARD_ON else []
            thicken_cards(cards, use[:CARD_MAX])
            # 보내기 전 원문 대조 검증: 원문과 다르거나 근거 없는 문장은 음성·카드에서 빼고 따로 알림
            raw_script, cards, removed, verified = verify_briefing(raw_script, cards, use, now, closing_text(lab, now, more),
                                                                  intro_text(lab, now))
        warn = ("" if verified is True else "⚠️ <b>자동 검증 결과 불확실 · 공유 전 확인 필요</b>\n" if verified == "over"
                else "⚠️ <b>자동 검증 실패 · 공유 전 확인 필요</b>\n")
        state["audio_verify"] = (state.get("audio_verify", []) + [[now.isoformat(), verified, removed]])[-10:]
        script = speakable(raw_script)             # 글 원고(텔레그램에 보임): 원래 표기
        spoken = speakable(raw_script, sound=True) # 음성용: 소리 나는 대로(영장→영짱 등)
        lines = [f"{n}. {html.escape(g[0]['title'][:60])} ({html.escape(g[0]['press'])})"
                 + (f" 외 {len(g) - 1}건" if len(g) > 1 else "") for n, g in enumerate(use, 1)]
        n_rewrite = sum(1 for e_ in AUDIO_ERR if "불일치" in e_)
        check = (f"🔎 자동 점검: 본문 확인 {n_body}/{n_all}건 · "
                 + ("원문 검증 통과" if verified is True and not removed else
                    f"원문 검증에서 {len(removed)}개 뺌" if verified is True else "원문 검증 불확실")
                 + f" · 자동 교정 {len(FIXES) + n_rewrite}건"
                 + (f"\n⚠️ 본문 미확인(제목 기준) 사안: {', '.join(map(str, title_only))}번" if title_only else ""))
        caption = warn + head + f"{len(recs)}건 · {len(use)}개 사안\n" + "\n".join(lines)
        if more:
            caption += f"\n외 {more}개 사안"
        if dropped_use:
            caption += "\n📎 본문 미확인으로 원고 제외: " + ", ".join(html.escape(g[0]["title"][:30]) for g in dropped_use)
        caption += "\n" + check
        if len(caption) > 1000:                    # 텔레그램 설명란 1,024자 제한(점검 줄은 남김)
            body_ = caption[:-len(check)]
            caption = body_[:990 - len(check)].rsplit("\n", 1)[0] + "\n…\n" + check
        # 원고 보관(최근 10회, state.json의 audio_scripts) → 나중에 글로 다시 볼 수 있게
        state["audio_scripts"] = (state.get("audio_scripts", []) + [[now.isoformat(), label, script]])[-10:]
        path = os.path.join(os.getenv("RUNNER_TEMP") or ".", f"중수청브리핑_{now:%m%d_%H%M}.mp3")
        try:
            engine = tts_mp3(spoken, path)
            mixed = add_bgm(path, now)
            if mixed != path:
                engine += " + 배경음악(" + BGM_USED[0] + ")"
                path = mixed
            state["audio_scripts"][-1].append(engine)
            mid = send_audio(path, caption, f"{now:%m.%d.} {label} 중수청 브리핑")
            if (os.getenv("AUDIO_SCRIPT_MSG") or "on").strip().lower() != "off":
                try:
                    send_script(script, mid, "📝 <b>음성 원고</b> (눌러서 펼치기 · 문단 번호로 지적해 주세요)\n")
                except Exception as e:
                    print(f"[원고 발송 오류] {type(e).__name__}: {e}", file=sys.stderr)
            try:
                send_removed(removed, mid, verified)
            except Exception as e:
                print(f"[검증 알림 오류] {type(e).__name__}: {e}", file=sys.stderr)
            if BRIEF_MODE == "both" and v2 is not None:
                try:
                    send_v2_compare(v2, use, label, mid)
                    state["v2_scripts"] = (state.get("v2_scripts", []) + [[now.isoformat(), label, v2.get("script", ""),
                                                                           v2.get("log", []), v2.get("title_only", [])]])[-10:]
                except Exception as e:
                    print(f"[새 방식 비교 발송 오류] {type(e).__name__}: {e}", file=sys.stderr)
            print(f"음성 브리핑({label}) 발송: {len(recs)}건, 원고 {len(script)}자, {engine}")
            if CARD_ON:
                try:
                    card_briefing(use, label, now, len(recs), more, mid, cards, warn, since, now)
                except Exception as e:
                    AUDIO_ERR.append(f"카드뉴스: {type(e).__name__}: {str(e)[:200]}")
                    print(f"[카드뉴스 실패] {type(e).__name__}: {e}", file=sys.stderr)
        except Exception as e:                     # 음성 실패 시 원고를 글로 대신 보냄
            print(f"[음성 생성 실패] {type(e).__name__}: {e}", file=sys.stderr)
            msg = head + f"{len(recs)}건 (음성 생성 실패, 원고로 대신 보냅니다)\n\n" + html.escape(script)
            for i in range(0, len(msg), 3900):
                send_telegram(msg[i:i + 3900])
    if AUDIO_ERR:
        state["audio_errors"] = (state.get("audio_errors", []) + [[now.isoformat()] + AUDIO_ERR[-8:]])[-10:]
    if label != "시험":                            # 시험 발송은 다음 정기 브리핑 범위에 영향 없음
        state["audio_cut"] = now.isoformat()
        state["last_audio"] = now.isoformat()


# ── 실행 ─────────────────────────────────────────────────────
def main() -> int:
    missing = [k for k, v in {"TELEGRAM_BOT_TOKEN": TG_TOKEN, "TELEGRAM_CHAT_ID": TG_CHAT}.items() if not v]
    if missing:
        print("필수 설정 누락: " + ", ".join(missing), file=sys.stderr)
        return 1

    now = dt.datetime.now(KST)
    if in_quiet_hours(now):
        print(f"{now:%H:%M} 방해금지 시간 - 건너뜀")
        return 0

    seen = load_seen()
    state = load_state()
    if BRIEF_FILE:                                 # Claude 원고 발송 실행: 기사 수집 없이 음성·카드만
        manual_briefing(state, now, BRIEF_FILE)
        save_seen(seen, now)
        save_state(state)
        return 0
    arts = collect(seen, now, state.get("last_run", ""))
    decode_links(arts)
    arts = dedupe(arts, seen, state, now)
    cutoff = now - dt.timedelta(hours=12)
    RECENT_SENT[:] = [r[0] for r in state.get("recent", []) if dt.datetime.fromisoformat(r[1]) >= cutoff] + \
        [p["title"] for p in state.get("pending", [])]
    for i in range(0, len(arts), BATCH):           # 기사가 많으면 나눠서 요약·판단
        summarize(arts[i:i + BATCH])

    for a in arts:                                 # 무관 기사도 다시 안 보도록 기록(번호·제목·주소 모두)
        seen[a["key"]] = now.isoformat()
        seen["t:" + norm_title(a["title"])] = now.isoformat()
        seen["u:" + a["url"]] = now.isoformat()
    olds = [a for a in arts if a.get("old")]
    if olds:
        print(f"지난 기사 {len(olds)}건 제외: " + " / ".join(a["title"][:30] for a in olds))
    dups = [a for a in arts if a["relevant"] and a.get("dup")]
    if dups:
        print(f"이미 보낸 기사와 같은 사안 {len(dups)}건 제외: " + " / ".join(a["title"][:30] for a in dups))
    scoops = [a for a in arts if a["relevant"] and is_scoop(a["title"])]
    if scoops:
        print(f"[단독] {len(scoops)}건은 중복 판단 없이 통과: " + " / ".join(a["title"][:30] for a in scoops))
    for a in arts:                                 # 거른 이유 기록
        if not a["relevant"]:
            drop(a.get("why") or "관련 없음", a["press"], a["title"], now)
        elif a.get("dup"):
            drop("AI 중복(이미 보낸 사안)", a["press"], a["title"], now)
    arts = [a for a in arts if a["relevant"] and not a.get("dup")]
    state["recent"] = [r for r in state.get("recent", [])
                       if now - dt.datetime.fromisoformat(r[1]) < dt.timedelta(hours=MAX_AGE_HOURS)]
    state["recent"] += [[a["title"], now.isoformat()] for a in arts]

    # ① 중수청 기사: 바로 발송
    urgent = [a for a in arts if a["priority"]]    # 건수 제한 없음(긴 경우 여러 통으로 나눠 발송)
    if urgent:
        head = f"⚡ <b>중수청 언론보도</b>\n{now:%m.%d. %H:%M} 기준 신규 {len(urgent)}건\n"
        for msg in format_messages(urgent, now, head):
            send_telegram(msg)
        print(f"중수청 {len(urgent)}건 즉시 발송")
        state.setdefault("audio_log", []).extend(to_record(a, now) for a in urgent)
    # ② 공소청·형사사법 기사: 대기 목록에 쌓았다가 마지막 모음 발송 후 DIGEST_MINUTES 지나면 발송
    shown = urgent
    for a in arts:
        if not a["priority"]:
            if any(similar(a["title"], b["title"]) for b in shown):
                continue                           # 방금 즉시 발송한 기사와 같은 내용이면 모음에서 제외
            state["pending"].append(to_record(a, now))
    last = dt.datetime.fromisoformat(state["last_digest"]) if state["last_digest"] else None
    due, nxt = digest_due(last, now)
    due = due or DIGEST_TEST
    if state["pending"] and due:
        items = []
        for p_ in state["pending"]:
            p_ = from_record(p_)
            p_["priority"] = False
            # 대기 중 들어온 기사끼리는 AI 묶음 번호가 실행마다 달라서 제목 유사도로 묶음
            p_["group"] = None if len({x.get("found") for x in state["pending"]}) > 1 else p_.get("group")
            items.append(p_)
        since = last.strftime("%H:%M") if last and last.date() == now.date() else "직전"
        head = (f"📚 <b>공소청·형사사법 모음</b> {since}~{now:%H:%M} · {len(items)}건"
                + {"fold": " (눌러서 펼치기)", "list": " · 제목을 누르면 기사"}.get(DIGEST_STYLE, "") + "\n")
        page = ""
        if DIGEST_STYLE == "page":                     # 웹페이지 한 장으로 만들고 대화방엔 링크 한 줄만
            try:
                n_grp = len(group_articles(items))
                page = telegraph_page(items, now, f"공소청·형사사법 모음 {now:%m.%d.} {since}~{now:%H:%M} ({len(items)}건)",
                                      f"{now:%Y.%m.%d.} {since}~{now:%H:%M} 신규 {len(items)}건 · {n_grp}개 사안 · 언론보도 요약(사실관계 확인 전)")
            except Exception as e:
                print(f"[모음 페이지 실패 → 글로 보냄] {type(e).__name__}: {e}", file=sys.stderr)
        if page:
            send_telegram(f"📚 <b>공소청·형사사법 모음</b> {since}~{now:%H:%M} · {len(items)}건\n"
                          f"<a href=\"{html.escape(page, quote=True)}\">▶ 모음 보기 (요약·기사 링크)</a>", silent=True, preview=True)
        else:
            msgs = (format_list(items, now, head) if DIGEST_STYLE == "list" else
                    format_messages(items, now, head, collapse=(DIGEST_STYLE == "fold")))
            for msg in msgs:
                send_telegram(msg, silent=True)        # 알림 소리 없이(중수청 기사만 울리게)
        print(f"공소청·형사사법 {len(items)}건 모음 발송")
        state["pending"] = []
        state["last_digest"] = now.isoformat()
    elif state["pending"]:
        print(f"공소청·형사사법 {len(state['pending'])}건 대기 중 (다음 모음 {nxt})")
    if not arts:
        print("새 기사 없음")

    try:
        audio_briefing(state, now)
    except Exception as e:                         # 음성 문제로 알림 전체가 멈추지 않게
        print(f"[음성 브리핑 오류] {type(e).__name__}: {e}", file=sys.stderr)
    keep_a = now - dt.timedelta(days=7)            # 음성용 기록은 7일·최대 300건만 보관(연휴 대비)
    state["audio_log"] = [r for r in state.get("audio_log", [])
                          if dt.datetime.fromisoformat(r["found"]) >= keep_a][-300:]
    for r in state["audio_log"]:                   # 원문 앞부분은 3일만 보관(저장 용량)
        if "body" in r and dt.datetime.fromisoformat(r["found"]) < now - dt.timedelta(days=3):
            r["body"] = ""

    state.pop("daily", None), state.pop("daily_sent", None)   # 예전 대응 검토 기록 정리
    state["last_run"] = now.isoformat()
    keep = now - dt.timedelta(hours=48)            # 거른 기사 기록은 48시간·최대 300건만 보관
    old_d = [r for r in state.get("dropped", []) if dt.datetime.fromisoformat(r[0]) >= keep]
    have = {(r[1], r[3]) for r in old_d}
    new_d = []
    for r in DROPPED:                              # 네이버는 매 실행 다시 걸러지므로 같은 기록은 한 번만
        if (r[1], r[3]) not in have:
            have.add((r[1], r[3]))
            new_d.append(r)
    state["dropped"] = (old_d + new_d)[-300:]
    save_seen(seen, now)                           # 발송 성공 후에만 기록
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
