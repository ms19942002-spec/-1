# AGENTS.md (GPT / Codex 용)

이 저장소에서 PR을 올리거나 리뷰할 때는 **반드시 `docs/agent-pr-protocol.md`의 규칙을 따르세요.**
Claude도 같은 규칙을 따르므로, 두 에이전트가 서로 PR과 리뷰를 주고받을 수 있습니다.

핵심 요약:
- 코멘트 맨 앞에 `[리뷰 요청]`, `[리뷰 결과: 변경 필요]`, `[리뷰 결과: 통과]`, `[수정 완료 N/3]`, `[머지 준비 완료]` 같은 표기를 붙입니다.
- 지적에는 🔴 필수 / 🟡 선택 심각도를 붙입니다.
- 왕복은 최대 3회입니다. 승인과 머지는 사람이 합니다.
- 다른 에이전트의 브랜치에 직접 푸시하지 않습니다.
- 모든 설명과 코멘트는 한국어로 작성합니다.

## 프로젝트
- `artifact-orchestrator/`: 사내 산출물 진단·관리 웹앱 (Python, FastAPI). 테스트: `cd artifact-orchestrator && pip install -r requirements-dev.txt && pytest -q`
  - 사내 설치형 LLM만 사용하며, **외부로 코드나 데이터를 보내는 변경은 금지**입니다.
- `lib/`, `pubspec.yaml`: Flutter 앱 (SRT 도착 알람)
