# Planning Insight Weekly Report

서비스 기획자, PM, PO를 위한 주간 Product Intelligence 리포트를 생성하고 Slack/Email로 발송하는 GitHub Actions 기반 자동화입니다.

## 기능

- PM/PO·서비스 기획 관련 RSS/콘텐츠 소스 수집
- 원문 페이지 excerpt 스크래핑
- 주간 기간 필터링
- Product Intelligence Analyst 프롬프트 구조에 맞춘 결과 생성
- JSON 출력 + 상세 Markdown 리포트 + Slack용 1,200자 이내 요약 메시지 생성
- Slack Bot Token 또는 Incoming Webhook 발송
- SMTP 이메일 발송

## 출력 파일

- `last_report.json`: 요청 프롬프트의 JSON 스키마 결과
- `last_report.md`: 상세 기획·제품 인사이트 리포트
- `last_slack_message.md`: Slack 채널 게시용 요약 메시지

## 수집 소스

설정 파일: `sources.json`

초기 소스는 다음을 포함합니다.

- SVPG
- Intercom Blog
- Lenny's Newsletter
- Product Talk
- Roman Pichler
- Product Coalition
- Atlassian Work Life

RSS가 깨진 XML로 내려오는 경우가 있어, 스크립트는 XML 파싱 실패 시 느슨한 RSS item 추출 fallback을 사용합니다. 각 원문 URL은 가능한 경우 본문 excerpt를 추가로 스크래핑합니다.

## 실행 주기

`.github/workflows/weekly-planning-insight.yml`

```yaml
cron: "0 23 * * 0"
```

매주 월요일 오전 8시(KST)에 실행됩니다. 기본 기간은 KST 기준 전주 월요일~일요일입니다.

## GitHub Secrets

### Slack - TA bot 방식 권장

| Secret | 설명 |
|---|---|
| `SLACK_BOT_TOKEN` | Slack Bot User OAuth Token. `xoxb-...` 형식 |
| `SLACK_CHANNEL_ID` | 리포트를 보낼 채널 ID. `C...` 또는 `G...` 형식 |
| `SLACK_WEBHOOK_URL` | 선택. Bot Token이 없을 때 fallback으로 사용 |

설정 순서:

1. Slack에서 리포트용 채널을 생성합니다.
2. 채널에 `TA bot`을 초대합니다. 예: `/invite @TA bot`
3. TA bot Slack App에 `chat:write` 권한이 있는지 확인합니다.
4. 채널 링크에서 `archives/C...` 또는 `archives/G...` 부분의 채널 ID를 복사합니다.
5. GitHub repo Settings → Secrets and variables → Actions에 `SLACK_BOT_TOKEN`, `SLACK_CHANNEL_ID`를 추가합니다.

보안 주의: Bot Token이 채팅/문서에 노출되면 Slack에서 rotate/revoke 후 새 토큰을 GitHub Secret에 저장하세요.

### Email

| Secret | 설명 | 예시 |
|---|---|---|
| `SMTP_HOST` | SMTP 서버 | `smtp.gmail.com` |
| `SMTP_PORT` | SMTP 포트 | `587` |
| `SMTP_USERNAME` | SMTP 로그인 계정 | `yourname@gmail.com` |
| `SMTP_PASSWORD` | SMTP 비밀번호/앱 비밀번호 | Gmail 앱 비밀번호 권장 |
| `SMTP_USE_SSL` | SSL 직접 연결 여부 | `false` for 587 |
| `EMAIL_FROM` | 발신 이메일 | `yourname@gmail.com` |
| `EMAIL_TO` | 수신 이메일. 미설정 시 기본값 사용 | `minseok.cho@unitblack.co.kr,jellasi@naver.com` |

## 수동 실행

GitHub Actions → `Weekly planning insight report` → `Run workflow`

특정 기간 테스트:

```text
period_from=2026-06-23
period_to=2026-06-28
notify=true
```

로컬 테스트:

```bash
python -m py_compile monitor.py
python monitor.py --period-from 2026-06-23 --period-to 2026-06-28
```

발송까지 테스트하려면 필요한 환경변수를 설정한 뒤:

```bash
python monitor.py --period-from 2026-06-23 --period-to 2026-06-28 --notify
```

## JSON 출력 스키마

```json
{
  "report_title": "리포트 제목",
  "report_period": "리포트 기간",
  "executive_summary": "전체 요약",
  "top_topics": [
    {
      "topic": "주제",
      "priority": "HIGH | MEDIUM | LOW",
      "summary": "핵심 내용",
      "practical_implication": "실무 시사점",
      "source_url": "출처 URL"
    }
  ],
  "detailed_report_markdown": "상세 리포트",
  "slack_message_markdown": "Slack 게시용 메시지",
  "requires_team_discussion": false
}
```
