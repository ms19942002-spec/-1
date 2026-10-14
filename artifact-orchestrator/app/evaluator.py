"""규칙 기반 점수 + LLM 평가 + 사람이 입력한 업무 정보를 합쳐 최종 판정을 만든다."""
import math
from datetime import datetime
from pathlib import Path

from . import analyzer, config, db, llm

VERDICTS = {
    "Invest": "투자 확대",
    "Tolerate": "현상 유지",
    "Migrate": "개선·통합",
    "Eliminate": "정리·폐기",
}
MATURITY = {1: "아이디어/스케치", 2: "프로토타입", 3: "파일럿", 4: "운영 가능", 5: "전사 표준"}
TECH_KEYS = ("completeness", "code_quality", "maintainability", "security")
WEB_KINDS = {"fastapi", "flask", "django", "node", "python-ui"}


def _clamp(v: float) -> int:
    return int(max(0, min(100, round(v))))


def heuristic_scores(a: dict) -> dict:
    """LLM 이 없어도 동작하는 객관 지표 기반 점수 (0~100)."""
    is_web = a["kind"] in WEB_KINDS
    has_surface = bool(a["endpoints"] or a["pages"])

    completeness = 25 + 15 * has_surface + 10 * a["has_readme"] + 10 * a["has_requirements"] \
        + 10 * a["has_dockerfile"] + 10 * a["has_tests"] + 5 * a["has_env_example"] \
        + min(15, a["total_loc"] / 100)

    code_quality = 60 + 15 * a["has_tests"] + 5 * a["uses_logging"] \
        - min(15, a["todo_count"] * 1.5) - min(20, len(a["big_files"]) * 5)
    if a["total_loc"] == 0:
        code_quality = 20

    recent = False
    if a.get("last_modified"):
        recent = (datetime.now() - datetime.fromisoformat(a["last_modified"])).days <= 180
    maintainability = 35 + 20 * a["has_readme"] + 15 * a["has_requirements"] + 10 * a["has_tests"] \
        + 10 * a["has_dockerfile"] + 10 * recent

    security = 85 - min(70, len(a["secrets"]) * 25) - 5 * bool(a["external_script_hosts"])
    if is_web:
        security += 10 if a["has_auth"] else -15

    return {"completeness": _clamp(completeness), "code_quality": _clamp(code_quality),
            "maintainability": _clamp(maintainability), "security": _clamp(security)}


def _maturity_from(completeness: int) -> int:
    for level, bound in ((1, 40), (2, 55), (3, 70), (4, 85)):
        if completeness < bound:
            return level
    return 5


def verdict_for(technical: float, business: float) -> str:
    if business >= 60:
        return "Invest" if technical >= 60 else "Migrate"
    return "Tolerate" if technical >= 60 else "Eliminate"


def finalize(artifact: dict, heuristic: dict, llm_result: dict | None) -> dict:
    """규칙 점수와 LLM 점수를 50:50 으로 섞고, 업무 가치는 사람 입력을 우선한다."""
    llm_scores = (llm_result or {}).get("scores") or {}
    blended = {}
    for k in TECH_KEYS:
        v = llm_scores.get(k)
        blended[k] = _clamp((heuristic[k] + float(v)) / 2) if isinstance(v, (int, float)) else heuristic[k]
    technical = sum(blended.values()) / len(blended)

    signals, sources = [], []
    if artifact.get("importance"):
        signals.append(int(artifact["importance"]) * 20)
        sources.append("업무 중요도(입력)")
    if artifact.get("monthly_users") is not None:
        # 1명→0, 10명→50, 100명 이상→100 (로그 스케일)
        signals.append(_clamp(math.log10(max(1, int(artifact["monthly_users"]))) * 50))
        sources.append("월간 사용자(입력)")
    if isinstance(llm_scores.get("business_value"), (int, float)):
        signals.append(float(llm_scores["business_value"]))
        sources.append("AI 추정")
    business = sum(signals) / len(signals) if signals else 50
    if not signals:
        sources.append("정보 없음(기본값 50)")

    maturity = (llm_result or {}).get("maturity")
    if not isinstance(maturity, int) or not 1 <= maturity <= 5:
        maturity = _maturity_from(blended["completeness"])

    v = verdict_for(technical, business)
    return {
        "scores": blended,
        "technical": _clamp(technical),
        "business": _clamp(business),
        "business_sources": sources,
        "overall": _clamp(technical * 0.5 + business * 0.5),
        "maturity": maturity,
        "maturity_label": MATURITY[maturity],
        "verdict": v,
        "verdict_label": VERDICTS[v],
        "ai_verdict": (llm_result or {}).get("verdict") if (llm_result or {}).get("verdict") in VERDICTS else None,
    }


def evaluate(artifact_id: int) -> None:
    art = db.get(artifact_id)
    if art is None:
        return
    db.update(artifact_id, status="analyzing", error="")
    try:
        path = Path(art["local_path"])
        if not path.exists():
            raise FileNotFoundError(f"분석 대상이 없습니다: {path}")
        analysis = analyzer.analyze(path)
        heur = heuristic_scores(analysis)
        llm_result, llm_status = None, "ok"
        try:
            excerpts = analyzer.key_files(path, config.settings.max_context_chars)
            llm_result = llm.chat_json(llm.EVAL_SYSTEM, llm.evaluation_prompt(art, analysis, excerpts))
        except llm.LLMError as e:
            llm_status = str(e)
        evaluation = {"heuristic": heur, "llm": llm_result, "llm_status": llm_status,
                      "model": config.settings.llm_model if llm_result else None,
                      "final": finalize(art, heur, llm_result)}
        db.update(artifact_id, analysis=analysis, evaluation=evaluation, status="done",
                  evaluated_at=db.now())
    except Exception as e:  # 한 건 실패가 전체 일괄 평가를 멈추지 않도록
        db.update(artifact_id, status="error", error=f"{type(e).__name__}: {e}")


def rescore(artifact_id: int) -> None:
    """메타정보(중요도, 사용자 수)만 바뀐 경우 LLM 재호출 없이 최종 점수만 다시 계산."""
    art = db.get(artifact_id)
    if not art or not art.get("evaluation"):
        return
    ev = art["evaluation"]
    ev["final"] = finalize(art, ev["heuristic"], ev.get("llm"))
    db.update(artifact_id, evaluation=ev)
