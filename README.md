# 중수청·공소청 언론보도 텔레그램 알림

30분마다 구글 뉴스에서 '중수청', '중대범죄수사청', '공소청' 새 기사를 찾아
요지 요약과 기사 링크를 텔레그램으로 보냅니다. 새 기사가 없으면 아무것도 보내지 않습니다.
(구글 뉴스는 연합뉴스·뉴스1·뉴시스·주요 일간지·방송사 등 국내 언론 기사를 모아 보여줍니다. 별도 가입이나 키가 필요 없습니다.)

```
📰 중수청·공소청 언론보도
11.03. 09:30 기준 신규 2건

■ 중수청
1. 중수청, ○○ 사건 첫 압수수색
   연합뉴스 · 09:12
   ▸ 중수청이 ○○ 관련 첫 강제수사에 착수함.
   기사 보기

■ 공소청 등
2. ...
```

## 준비물

### 1. 텔레그램 봇 (완료)
- `TELEGRAM_BOT_TOKEN`: BotFather가 준 토큰
- `TELEGRAM_CHAT_ID`: getUpdates로 확인한 채팅 ID 숫자

### 2. (선택) Claude API 키: 본문 기반 AI 요약
- https://console.anthropic.com 에서 API 키를 발급받습니다 → `ANTHROPIC_API_KEY`
- 넣으면: 기사 본문을 읽어 1~2문장 개조식 요지를 쓰고, 이름만 겹친 무관한 기사는 자동으로 뺍니다.
- 안 넣으면: 기사 본문의 첫머리(약 120자)를 대신 보여줍니다. 완전 무료입니다.

### 3. GitHub 저장소 등록
1. GitHub에서 **New repository**를 누르고, 이름(예: `news-alert`)을 정한 뒤 **Public**을 선택해 생성합니다.
   - Public이어도 키는 Secrets에 저장되므로 공개되지 않습니다. Private이면 무료 실행시간(월 2,000분)이 빠듯합니다.
2. **Add file → Upload files**로 `news_alert.py`, `requirements.txt`, `README.md`를 올립니다.
3. **Add file → Create new file**을 누르고, 파일 이름 칸에 `.github/workflows/news-alert.yml`을 입력합니다.
   같은 이름 파일의 내용을 붙여넣고 Commit합니다.
4. **Settings → Secrets and variables → Actions → New repository secret**에 아래 값을 등록합니다.
   - `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
   - `ANTHROPIC_API_KEY` (선택)
5. **Settings → Actions → General → Workflow permissions**에서 **Read and write permissions**를 선택하고 저장합니다.
   (보낸 기사 기록을 저장하는 데 필요합니다.)
6. **Actions 탭 → 중수청·공소청 뉴스 알림 → Run workflow**로 시험 실행합니다.
   - 첫 실행은 최근 2시간 기사만 보냅니다. 그 뒤로는 새 기사만 보냅니다.

## 선택 설정 (Settings → Secrets and variables → Actions → **Variables** 탭)
| 이름 | 예시 | 설명 |
|---|---|---|
| `QUIET_HOURS` | `0-6` | 0시~6시에는 보내지 않고, 7시 첫 실행 때 모아서 보냅니다 |
| `QUERIES` | `중수청,중대범죄수사청,공소청,검찰개혁` | 검색어(쉼표로 구분) |

## 참고
- GitHub 예약 실행은 서버 사정으로 정각보다 5~15분 늦을 수 있습니다.
- 구글 뉴스에 반영되기까지 기사 게재 후 수 분~수십 분이 걸릴 수 있습니다.
- 보낸 기사 목록은 `seen.json`에 자동 저장되어 같은 기사를 두 번 보내지 않습니다(7일 보관).
- 공개된 언론 기사만 다루며, 업무망이나 내부 자료와는 연결되지 않습니다.
