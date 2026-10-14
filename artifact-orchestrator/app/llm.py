"""사내 설치형 LLM 클라이언트.

OpenAI 호환 /chat/completions 엔드포인트만 사용하므로 Ollama, vLLM, LM Studio,
llama.cpp server, TGI 등 어느 것이든 사내 서버에 띄워 연결하면 된다.
코드는 AO_LLM_BASE_URL 로 지정한 주소 외에는 어디로도 전송되지 않는다.
"""
import json
import re

import httpx

from . import config


class LLMError(Exception):
    pass


def _extract_json(text: str) -> dict:
    text = re.sub(r"<think>[\s\S]*?</think>", "", text).strip()  # 추론형 모델의 사고 과정 제거
    try:
        return json.loads(text)
    except ValueError:
        pass
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if m:
        try:
            return json.loads(m.group(1))
        except ValueError:
            pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except ValueError:
            pass
    raise LLMError("모델 응답에서 JSON 을 찾지 못했습니다")


def chat_json(system: str, user: str) -> dict:
    s = config.settings
    if not s.llm_enabled:
        raise LLMError("LLM 비활성화 (AO_LLM_ENABLED=false)")
    payload = {
        "model": s.llm_model,
        "temperature": 0.2,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {s.llm_api_key}"}
    url = f"{s.llm_base_url}/chat/completions"
    try:
        with httpx.Client(timeout=s.llm_timeout, trust_env=False) as client:
            r = client.post(url, json=payload, headers=headers)
            if r.status_code == 400:  # response_format 미지원 서버 대비
                payload.pop("response_format")
                r = client.post(url, json=payload, headers=headers)
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
    except (httpx.HTTPError, KeyError, IndexError, ValueError) as e:
        raise LLMError(f"LLM 호출 실패 ({url}): {e}") from e
    return _extract_json(content)


def ping() -> tuple[bool, str]:
    s = config.settings
    if not s.llm_enabled:
        return False, "비활성화"
    try:
        with httpx.Client(timeout=2, trust_env=False) as client:
            r = client.get(f"{s.llm_base_url}/models", headers={"Authorization": f"Bearer {s.llm_api_key}"})
            r.raise_for_status()
        return True, f"{s.llm_model} @ {s.llm_base_url}"
    except httpx.HTTPError as e:
        return False, f"연결 실패: {s.llm_base_url} ({type(e).__name__})"


EVAL_SYSTEM = """당신은 사내 IT 포트폴리오를 심사하는 시니어 아키텍트입니다.
직원들이 각자 만든 웹앱(FastAPI 등)과 HTML 산출물을 평가해 계속 투자할 가치가 있는지,
어떻게 발전시키면 좋을지 판단합니다. 근거는 반드시 제공된 분석 결과와 코드에서만 찾고,
모르는 것은 추측하지 말고 '확인 필요'라고 쓰십시오. 모든 문장은 한국어로 작성합니다.
반드시 아래 스키마의 JSON 하나만 출력하십시오.

{
  "summary": "이 산출물이 무엇을 하는지 2~3문장",
  "purpose": "해결하려는 업무 문제 한 문장",
  "target_users": "예상 사용자 (예: 영업팀, 생산관리자)",
  "maturity": 1~5 정수 (1 아이디어/스케치, 2 프로토타입, 3 파일럿, 4 운영 가능, 5 전사 표준 수준),
  "scores": {
    "completeness": 0~100, "code_quality": 0~100, "maintainability": 0~100,
    "security": 0~100, "business_value": 0~100
  },
  "strengths": ["..."],
  "risks": ["..."],
  "verdict": "Invest | Tolerate | Migrate | Eliminate",
  "verdict_reason": "판정 근거 2~3문장",
  "next_steps": ["우선순위 순서의 구체적 개발 과제 3~6개"],
  "integration_hint": "다른 시스템과 합칠 때 공통화할 만한 부분 (데이터, API, 화면 등)"
}

판정 기준(Gartner TIME):
- Invest: 업무 가치 높고 기술 상태 양호 → 확대 투자
- Tolerate: 기술 상태는 괜찮지만 가치가 제한적 → 현상 유지
- Migrate: 가치는 높지만 기술 상태가 나쁨 → 재개발/통합/이관
- Eliminate: 가치와 기술 모두 낮음 → 정리"""


def evaluation_prompt(artifact: dict, analysis: dict, excerpts: list[tuple[str, str]]) -> str:
    meta = {k: artifact.get(k) for k in ("name", "owner", "department", "description",
                                         "importance", "monthly_users") if artifact.get(k) not in (None, "")}
    facts = {k: v for k, v in analysis.items() if k not in {"keywords"}}
    parts = [
        "## 등록 정보", json.dumps(meta, ensure_ascii=False),
        "## 정적 분석 결과", json.dumps(facts, ensure_ascii=False)[:6000],
        "## 핵심 파일 발췌",
    ]
    for name, text in excerpts:
        parts.append(f"### {name}\n```\n{text}\n```")
    return "\n".join(parts)


PLAN_SYSTEM = """당신은 사내 시스템 통합을 설계하는 아키텍트입니다.
기능이나 데이터가 겹치는 여러 산출물의 정보를 보고 통합 방안을 제안합니다.
제공된 정보에만 근거하고 한국어로 작성하며, 반드시 아래 스키마의 JSON 하나만 출력하십시오.

{
  "should_merge": true | false,
  "overlap": "무엇이 겹치는지 (데이터, 기능, 화면)",
  "target_name": "통합 후 서비스 이름 제안",
  "base_artifact": "기반으로 삼을 산출물 이름과 이유",
  "architecture": "통합 구조 제안 (공통 API, 공통 DB 접근 계층, 화면 구성 등)",
  "steps": ["단계별 통합 작업 4~8개"],
  "retire": ["통합 후 정리할 산출물 이름"],
  "risks": ["통합 시 주의할 점"]
}"""


def plan_prompt(members: list[dict], shared: list[str]) -> str:
    rows = []
    for a in members:
        ev = (a.get("evaluation") or {})
        llm_part = ev.get("llm") or {}
        an = a.get("analysis") or {}
        rows.append({
            "name": a["name"], "owner": a.get("owner"), "department": a.get("department"),
            "kind": an.get("kind"), "summary": llm_part.get("summary") or a.get("description"),
            "endpoints": [f'{e["method"]} {e["path"]}' for e in an.get("endpoints", [])][:30],
            "pages": [p.get("title") for p in an.get("pages", [])][:15],
            "tables": an.get("tables", [])[:20],
            "final": ev.get("final"),
        })
    return ("## 공통 요소\n" + ", ".join(shared) + "\n## 산출물 목록\n"
            + json.dumps(rows, ensure_ascii=False, indent=1)[:12000])
