# CLAUDE.md — VCMS Report Generator Guidelines

## Core Principles (Karpathy-Inspired)

### 1. Think Before Coding
- State assumptions explicitly — uncertain하면 추측하지 말고 물어라
- 여러 해석이 가능하면 선택지를 제시하라
- 더 단순한 방법이 있으면 push back 하라
- 혼란스러우면 멈추고 뭐가 불명확한지 말하라

### 2. Simplicity First
- 요청된 것 이상의 기능 추가 금지
- 한 번만 쓰는 코드에 추상화 금지
- "유연성", "확장성" 미요청 시 불필요
- 200줄이 50줄로 가능하면 다시 써라
- 테스트: 시니어 개발자가 "이거 오버엔지니어링 아님?" 하면 → 단순화

### 3. Surgical Changes
- 요청된 부분만 수정하라
- 옆에 있는 코드/주석/포맷 "개선" 금지
- 안 깨진 것을 리팩토링 하지 마라
- 기존 스타일을 따르라
- 관련 없는 dead code 발견 시 → 삭제하지 말고 언급만
- 변경한 모든 라인은 유저 요청에 직접 연결되어야 함

### 4. Goal-Driven Execution
- 성공 기준을 먼저 정의하라
- "X 추가해" → "X에 대한 테스트 작성 → 통과시키기"
- 멀티스텝 작업 시 계획 먼저:
  ```
  1. [단계] → 검증: [확인사항]
  2. [단계] → 검증: [확인사항]
  ```

## Project-Specific Rules

### Report Generation
- Claude API 리포트 생성 시 사실만 기재 (할루시네이션 금지)
- 숫자 인용 시 반드시 출처 메시지 리스트 표기
- Slack mrkdwn 포맷 사용 (Markdown 아님)

### Architecture
- GitHub Actions: 스케줄러 + 리포트 생성
- Cloudflare Worker: 피드백 버튼 핸들링
- Notion: 리포트 아카이빙
- Slack: 입출력 채널

### Code Style
- Python: 함수별 단일 책임
- 에러 핸들링: 실패 시 WARNING 로그 후 계속 진행 (전체 중단 금지)
- 환경변수: 모든 시크릿은 env로 주입
