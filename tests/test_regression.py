"""오류 사례 시험 모음 — 지금까지 지적된 오류가 다시 나오지 않는지 확인(외부 접속 없이 실행)
실행: python tests/test_regression.py   (코드를 고칠 때마다 돌림)"""
import datetime as dt
import json
import os
import sys

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import news_alert as n  # noqa: E402

KST = n.KST
NOW = dt.datetime(2026, 11, 27, 7, 10, tzinfo=KST)
FAILS = []


def check(name, cond, detail=""):
    print(("통과 " if cond else "실패 ") + name + ("" if cond else f"  → {detail}"))
    if not cond:
        FAILS.append(name)


# ── 1. 사실 표현(11.24. '산하가 됐다', 11.27. '관할하게 됩니다') ──
t = n._plain("행안부는 경찰과 중수청을 관할하게 됩니다.", "x")
check("관할 변화 표현 교정", "관할하게" not in t and "관할합니다" in t, t)
check("카드 문구 관할 교정", n._fix_facts("행안부가 경찰과 중수청을 관할하게 됨") == "행안부가 경찰과 중수청을 관할함")

# ── 2. 평가·전망 표현(11.26.~27.) ──
t = n._plain("특활비는 공개되지 않습니다. 투명성 확보가 중요해질 것으로 보입니다.", "x")
check("앵커 단정 전망 문장 삭제", "보입니다" not in t, t)
t = n._plain("임 청장은 꿈이라고 강조했습니다. 기관장으로서는 드문 발언입니다.", "x")
check("'드문 발언' 삭제", "드문" not in t, t)
t = n._plain("경찰 관계자는 결과가 나올 것으로 보인다고 말했습니다.", "x")
check("누가 한 말인 전망은 유지", "말했습니다" in t, t)
check("'그쳤습니다' 교정", n._plain("약 4.2퍼센트에 그쳤습니다.", "x") == "약 4.2퍼센트였습니다.")
check("'그치고 있습니다' 교정", "그치고" not in n._plain("충원율은 30퍼센트 안팎에 그치고 있습니다.", "x"))
t = n._plain("특활비는 공개되지 않습니다. 그래서 투명성이 문제입니다.", "원문")
check("원문에 없는 '그래서' 삭제", "그래서" not in t, t)
t = n._plain("이 가운데 약 76퍼센트인 4억원을 사업에 배정했습니다.", "x")
check("숫자 겹치기 분리", "퍼센트인" not in t and "전체의 약 76퍼센트입니다" in t, t)

# ── 3. 날짜(11.26. '오늘 정례회의') ──
g = [{"pub": "2026-11-26T15:00:00+09:00"}]
check("기사 속 '오늘' → 작성일", n._fix_today("증선위는 오늘 정례회의에서 의결했습니다.", g, NOW).startswith("증선위는 26일"))

# ── 4. 원고 구조(11.26. 첫머리가 뒤에, '사안 1'이 남음, 넘김말 밀림) ──
raw = ("[사안 1] 첫 소식입니다.\n\n[사안 2] 둘째 소식입니다.\n\n11월 27일 금요일, 중수청 언론보도 아침 브리핑입니다.\n\n"
       "지금까지 중수청 언론보도 브리핑이었습니다.")
saved = n.VERIFY_ON
n.VERIFY_ON = False
out = n.verify_briefing(raw, [], [[{}], [{}]], NOW, n.closing_text("아침", NOW, 0), n.intro_text("아침", NOW))[0]
n.VERIFY_ON = saved
paras = out.split("\n\n")
check("첫머리가 맨 앞", paras[0] == n.intro_text("아침", NOW), paras[0])
check("첫 사안에 '먼저'", paras[1].startswith("먼저, 첫 소식"), paras[1])
check("마지막 사안에 '마지막 소식'", paras[2].startswith("마지막 소식입니다. 둘째"), paras[2])
check("'[사안' 표시 안 남음", "[사안" not in out and "사안 1" not in out)
check("첫머리 문구(아침 브리핑)", n.intro_text("아침", NOW) == "11월 27일 금요일, 중수청 언론보도 아침 브리핑입니다.")

# ── 5. 음성 발음(11.26.) ──
sp = lambda t: n.speakable(t, sound=True)  # noqa: E731
check("대전중수청 띄어 읽기", "대전 중수청" in sp("대전중수청은 수사합니다."))
check("소수 퍼센트", "사점이퍼센트" in sp("약 4.2퍼센트였습니다."))
check("정수 퍼센트", "칠십육퍼센트" in sp("약 76퍼센트입니다."))
check("천 단위 쉼표 금액", "오억 이천오백만 원" in sp("예산은 5억 2,500만 원입니다."))
check("날짜는 그대로", "11월" in sp("11월 26일에 발표했습니다."))
check("나열 쉼표는 나누지 않음",
      len(n._clauses("미래에셋증권, NH투자증권, 중소기업은행, KB증권, 키움증권 등 7개사는 고발됐습니다.")) == 1)

# ── 6. 새 방식: 사실 뽑기(근거 문장 글자 대조) ──
body = ("중대범죄수사청이 올해 예비비로 편성된 특수활동비 5억2500만원 가운데 4억원을 중대경제범죄수사 사업에 배정했다. "
        "중수청 관계자는 특활비는 기밀 유지가 필요하다고 말했다. ") * 2
use = [[{"title": "중수청 첫 특활비 신청", "press": "서울신문", "body": body, "summary": "", "url": "u",
         "pub": "2026-11-26T16:00:00+09:00"}],
       [{"title": "중수청 사건 쇼핑 우려", "press": "뉴스1", "body": "", "summary": "", "url": "v",
         "pub": "2026-11-27T06:00:00+09:00"}]]
fake_facts = {"1": [
    {"id": "1a", "text": "중수청은 특활비 5억2500만원 가운데 4억원을 중대경제범죄수사 사업에 배정함",
     "quote": "특수활동비 5억2500만원 가운데 4억원을 중대경제범죄수사 사업에 배정했다"},
    {"id": "1b", "text": "중수청은 특활비 10억원을 배정함", "quote": "특수활동비 10억원을 배정했다"},          # 본문에 없는 근거
    {"id": "1c", "text": "중수청은 특활비 7억원을 배정함", "quote": "4억원을 중대경제범죄수사 사업에 배정했다"},  # 근거에 없는 숫자
    {"id": "1d", "text": "중수청 관계자는 특활비는 기밀 유지가 필요하다고 말함",
     "quote": "중수청 관계자는 특활비는 기밀 유지가 필요하다고 말했다"}]}
calls = []


def fake_claude(prompt, max_tokens=8000, timeout=150, model=""):
    calls.append(prompt[:30])
    if "사실을 옮겨 적는 일" in prompt:
        return json.dumps(fake_facts, ensure_ascii=False)
    if "[확인된 사실]" in prompt:
        return json.dumps({"paras": [{"n": 1, "sents": [
            {"t": "중수청이 특수활동비 4억원을 중대경제범죄수사 사업에 배정했습니다", "f": ["1a"]},
            {"t": "중수청은 특활비 9억원을 추가로 요청했습니다", "f": ["1a"]},                       # 사실에 없는 숫자
            {"t": "중수청은 오늘 특활비 신청을 받았습니다", "f": ["1a"]},                           # 상대 날짜
            {"t": "중수청 관계자는 특활비는 기밀 유지가 필요하다고 말했습니다", "f": ["1d"]},
            {"t": "서울중앙지검도 특활비를 받았습니다", "f": ["1a"]}]}],                            # 사실에 없는 기관명
            "cards": [{"n": 1, "tag": "예산", "headline": {"t": "중수청 특활비 4억원 경제범죄 배정", "f": ["1a"]},
                       "points": [{"t": "특활비 5억2500만원 중 4억원 배정", "f": ["1a"]},
                                  {"t": "특활비 9억원 요청", "f": ["1a"]}]}]}, ensure_ascii=False)
    return "[]"                                                                                  # 원문 검증: 문제 없음


real = n.ask_claude
n.ask_claude = fake_claude
n.ANTHROPIC_KEY = n.ANTHROPIC_KEY or "test"
try:
    facts, title_only = n.extract_facts(use, NOW)
    ids = [f["id"] for f in facts.get(1, [])]
    check("근거 문장이 본문에 있는 사실만 남김", ids == ["1a", "1d"], ids)
    check("본문 없는 사안은 원고 제외", title_only == [2], title_only)
    v2 = n.brief_v2(use, "아침", NOW, 0)
    sc = v2["script"]
    check("새 방식: 확인된 문장 유지", "4억원을 중대경제범죄수사 사업에 배정했습니다" in sc, sc)
    check("새 방식: 사실에 없는 숫자 문장 삭제", "9억" not in sc, sc)
    check("새 방식: 상대 날짜 문장 삭제", "오늘 특활비" not in sc, sc)
    check("새 방식: 사실에 없는 기관명 문장 삭제", "서울중앙지검" not in sc, sc)
    check("새 방식: 첫머리·맺음말", sc.startswith("11월 27일 금요일, 중수청 언론보도 아침 브리핑입니다.")
          and "브리핑이었습니다" in sc, sc[:60])
    c = v2["cards"][0]
    check("새 방식 카드: 사실에 없는 요점 삭제", all("9억" not in p for p in c["points"]), c)
    check("새 방식 카드: tag 목록 밖이면 기타", c["tag"] == "기타", c["tag"])
    check("새 방식: 버린 기록 남김", len(v2["log"]) >= 4, v2["log"])
finally:
    n.ask_claude = real

# 11.27. 텍스트 알림에 언론사 대신 기사 주소가 나온 문제(같은 이름 함수가 두 번 정의돼 뒤엣것이 덮어씀)
check("언론사: 기사 주소 → 이름", n.press_name("https://www.newsis.com/view/NISX20261127_0003886344") == "뉴시스")
check("언론사: 도메인 → 이름", n.press_name("cctoday.co.kr") == "충청투데이")
check("언론사: 이름은 그대로", n.press_name("뉴시스") == "뉴시스")
check("언론사: 모르는 곳은 주소 대신 도메인", n.press_name("https://www.unknownpress.kr/a/1") == "unknownpress.kr")

# 11.27. 같은 사건이 언론사마다 다른 제목으로 계속 온 문제(AI 크레딧 부족으로 AI 중복 판단이 꺼졌을 때)
check("같은 사건: 제목 달라도 핵심 낱말 겹치면 중복", n.same_story("주가조작 뒤 8년 국외 도피…범LG가 3세 구본현 기소", "'145억 부당이득' 범LG가 3세 구본현, 8년 도피 끝 구속"))
check("같은 사건: 낸시랭 음주운전", n.same_story("낸시랭, 음주운전 혐의로 불구속 송치", "경찰, 방송인 낸시랭 '음주운전' 혐의 공소청에 송치"))
check("다른 사건: 중수청만 겹치면 아님", not n.same_story("중수청, 이달 말 변호사 경력 채용", "내란특수본, 내달 1일 출범 가닥…중수청 10여명, 경찰 60여명"))

check("후속 보도: 압수수색 뒤 구속영장 청구는 새 단계", n.is_followup("중수청, 김OO 대표 구속영장 청구", ["중수청, OO건설 김OO 대표 자택 압수수색"]))
check("후속 아님: 같은 구속 소식 다른 언론사", not n.is_followup("'145억 부당이득' 구본현, 8년 도피 끝 구속", ["범LG가 3세 구본현 구속 기소"]))
check("후속 아님: 불구속은 구속 단계로 안 봄", not n.is_followup("낸시랭 불구속 송치", ["낸시랭 음주운전 송치"]))
import cards_v2
_cs = [{"headline": f"사안{i}", "viz": {"type": "vs"}} for i in range(5)]
check("카드 장수 맞춤: 사안 5개·시각화 5개(13장) → 표지+묶음 한도 이내", 3 + sum(2 if c.get("viz") else 1 for c in cards_v2.fit_album(_cs)) <= cards_v2.ALBUM_MAX)
check("카드 10장 맞춤: 앞 사안 시각화는 남김", bool(cards_v2.fit_album(_cs)[0].get("viz")))
check("카드 사안 최대 8개", n.CARD_MAX == 8)
check("발음: 내란 특별수사본부 붙여 읽기", "내란특별수사본부를" in n.speakable("내란 특별수사본부를 출범합니다.", sound=True))
check("발음: 내란 특수본 붙여 읽기", "내란특수본" in n.speakable("내란 특수본이 출범합니다.", sound=True))
check("원고 글은 띄어쓰기 그대로", "내란 특별수사본부" in n.speakable("내란 특별수사본부를 출범합니다."))
check("모음: 제목 끝 언론사 꼬리 떼기", n.clean_title("중수청, 경비 지적에 해명 - 머니투데이") == "중수청, 경비 지적에 해명")
check("모음: 요약 안내문·송고 지우기", "송고" not in n.clean_summary("후보 재산 신고 전체 내용을 이해하기 위해서는 기사 본문과 함께 읽어야 합니다. 송고…"))
check("모음: 바이라인 지우기", "기자]" not in n.clean_summary("[헤럴드경제=최의종 기자] 내란 특수본이 다음 달 출범한다."))
check("언론사: 영문 표기 Chosunbiz → 조선비즈", n.press_name("Chosunbiz") == "조선비즈")
print(f"\n{'모두 통과' if not FAILS else '실패 ' + str(len(FAILS)) + '건: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
