"""산출물 오케스트레이터 웹앱 (FastAPI + Jinja2 HTML)."""
import csv
import io
import queue
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from . import config, db, evaluator, ingest, llm, similarity

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.globals["VERDICTS"] = evaluator.VERDICTS

# LLM 은 한 번에 하나씩 호출하도록 단일 작업자 스레드로 직렬 처리한다.
_jobs: "queue.Queue[int]" = queue.Queue()


def _worker() -> None:
    while True:
        artifact_id = _jobs.get()
        try:
            evaluator.evaluate(artifact_id)
        finally:
            _jobs.task_done()


def enqueue(artifact_id: int) -> None:
    db.update(artifact_id, status="pending")
    if config.settings.sync_eval:
        evaluator.evaluate(artifact_id)
    else:
        _jobs.put(artifact_id)


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init()
    if not config.settings.sync_eval:
        threading.Thread(target=_worker, daemon=True, name="evaluator").start()
        # 서버 재시작 전에 대기/진행 중이던 건 다시 큐에 넣는다
        for a in db.list_all():
            if a["status"] in {"pending", "analyzing"}:
                _jobs.put(a["id"])
    yield


app = FastAPI(title="산출물 오케스트레이터", lifespan=lifespan)


def _redirect(url: str, msg: str = "") -> RedirectResponse:
    from urllib.parse import quote
    return RedirectResponse(f"{url}?msg={quote(msg)}" if msg else url, status_code=303)


def register(name: str, source_type: str, source_ref: str, local_path: Path) -> tuple[int, bool]:
    existing = db.find_by_source(source_type, source_ref)
    if existing:
        return existing["id"], False
    return db.create(name, source_type, source_ref, str(local_path)), True


def _groups() -> list[dict]:
    arts = {a["id"]: a for a in db.list_all()}
    groups = similarity.find_groups([a for a in arts.values() if a["status"] == "done"],
                                    config.settings.similarity_threshold)
    for g in groups:
        g["artifacts"] = [arts[i] for i in g["members"]]
        g["plan"] = db.get_plan(g["key"])
    return groups


# ---------------------------------------------------------------- HTML 화면
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, msg: str = ""):
    arts = db.list_all()
    done = [a for a in arts if a["status"] == "done"]
    counts = {k: 0 for k in evaluator.VERDICTS}
    for a in done:
        key = a["decision"] or a["evaluation"]["final"]["verdict"]
        counts[key] = counts.get(key, 0) + 1
    groups = _groups()
    group_of = {i: n + 1 for n, g in enumerate(groups) for i in g["members"]}
    return templates.TemplateResponse(request, "index.html", {
        "artifacts": arts, "done": done, "counts": counts, "group_of": group_of,
        "pending": sum(a["status"] in {"pending", "analyzing"} for a in arts),
        "llm_status": llm.ping(), "msg": msg,
    })


@app.post("/scan")
def scan(root: str = Form(...)):
    try:
        candidates = ingest.discover(Path(root))
    except ingest.IngestError as e:
        return _redirect("/", str(e))
    new = 0
    for c in candidates:
        aid, created = register(c.name, "path", str(c.path.resolve()), c.path)
        if created:
            new += 1
            enqueue(aid)
    return _redirect("/", f"{len(candidates)}개 산출물 발견, 신규 {new}개 등록 → 평가 대기열에 추가")


@app.post("/artifacts")
async def add_artifact(source_type: str = Form(...), source_ref: str = Form(""), name: str = Form(""),
                       file: UploadFile | None = File(None)):
    try:
        if source_type == "upload":
            if file is None or not file.filename:
                raise ingest.IngestError("업로드할 파일을 선택하세요")
            local = ingest.save_upload(file.filename, await file.read())
            ref = f"{file.filename}#{local.parent.name if local.is_file() else local.name}"
            name = name or Path(file.filename).stem
        elif source_type == "path":
            local = ingest.check_allowed(Path(source_ref))
            ref = str(local)
        elif source_type == "git":
            local, ref = ingest.clone_git(source_ref.strip()), source_ref.strip()
        elif source_type == "url":
            local, ref = ingest.fetch_url(source_ref.strip()), source_ref.strip()
        else:
            raise ingest.IngestError("알 수 없는 등록 방식입니다")
    except ingest.IngestError as e:
        return _redirect("/", str(e))
    name = name or Path(ref.rstrip("/").split("#")[0]).stem or ref
    aid, created = register(name, source_type, ref, local)
    if created:
        enqueue(aid)
    return _redirect(f"/artifacts/{aid}", "등록 완료, 평가를 시작합니다" if created else "이미 등록된 산출물입니다")


@app.get("/artifacts/{artifact_id}", response_class=HTMLResponse)
def artifact_page(request: Request, artifact_id: int, msg: str = ""):
    art = db.get(artifact_id)
    if not art:
        raise HTTPException(404)
    group = next((g for g in _groups() if artifact_id in g["members"]), None)
    return templates.TemplateResponse(request, "artifact.html", {
        "a": art, "group": group, "msg": msg, "maturity": evaluator.MATURITY})


def _int_or_none(v: str) -> int | None:
    v = (v or "").strip().replace(",", "")
    return int(v) if v.isdigit() else None


@app.post("/artifacts/{artifact_id}/meta")
def update_meta(artifact_id: int, name: str = Form(...), owner: str = Form(""), department: str = Form(""),
                description: str = Form(""), importance: str = Form(""), monthly_users: str = Form(""),
                decision: str = Form(""), decision_note: str = Form("")):
    if not db.get(artifact_id):
        raise HTTPException(404)
    imp = _int_or_none(importance)
    db.update(artifact_id, name=name.strip() or "이름 없음", owner=owner, department=department,
              description=description, importance=imp if imp and 1 <= imp <= 5 else None,
              monthly_users=_int_or_none(monthly_users),
              decision=decision if decision in evaluator.VERDICTS or decision == "Hold" else "",
              decision_note=decision_note)
    evaluator.rescore(artifact_id)
    return _redirect(f"/artifacts/{artifact_id}", "저장했습니다")


@app.post("/artifacts/{artifact_id}/evaluate")
def reevaluate(artifact_id: int):
    if not db.get(artifact_id):
        raise HTTPException(404)
    enqueue(artifact_id)
    return _redirect(f"/artifacts/{artifact_id}", "재평가를 대기열에 넣었습니다")


@app.post("/artifacts/{artifact_id}/delete")
def remove(artifact_id: int):
    db.delete(artifact_id)
    return _redirect("/", "삭제했습니다 (원본 파일은 건드리지 않습니다)")


@app.get("/groups", response_class=HTMLResponse)
def groups_page(request: Request, msg: str = ""):
    return templates.TemplateResponse(request, "groups.html", {
        "groups": _groups(), "msg": msg, "threshold": config.settings.similarity_threshold})


@app.post("/groups/plan")
def make_plan(key: str = Form(...)):
    group = next((g for g in _groups() if g["key"] == key), None)
    if not group:
        return _redirect("/groups", "그룹이 바뀌었습니다. 새로고침 후 다시 시도하세요")
    try:
        plan = llm.chat_json(llm.PLAN_SYSTEM, llm.plan_prompt(group["artifacts"], group["shared"]))
    except llm.LLMError as e:
        return _redirect("/groups", str(e))
    db.save_plan(key, plan)
    return _redirect("/groups", "통합 방안을 생성했습니다")


# ---------------------------------------------------------------- JSON / CSV
@app.get("/api/artifacts")
def api_artifacts():
    return db.list_all()


@app.get("/api/artifacts/{artifact_id}")
def api_artifact(artifact_id: int):
    art = db.get(artifact_id)
    if not art:
        raise HTTPException(404)
    return art


@app.get("/api/groups")
def api_groups():
    return JSONResponse([{k: v for k, v in g.items() if k != "artifacts"} for g in _groups()])


@app.get("/export.csv")
def export_csv():
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["ID", "이름", "유형", "담당자", "부서", "출처", "성숙도", "기술점수", "업무가치",
                "종합", "AI판정", "최종결정", "요약", "다음 단계"])
    for a in db.list_all():
        ev = a.get("evaluation") or {}
        f, l = ev.get("final") or {}, ev.get("llm") or {}
        w.writerow([a["id"], a["name"], (a.get("analysis") or {}).get("kind", ""), a["owner"], a["department"],
                    a["source_ref"], f.get("maturity_label", ""), f.get("technical", ""), f.get("business", ""),
                    f.get("overall", ""), f.get("verdict_label", ""),
                    evaluator.VERDICTS.get(a["decision"], "보류" if a["decision"] == "Hold" else ""),
                    l.get("summary", ""), " / ".join(l.get("next_steps") or [])])
    data = "﻿" + buf.getvalue()  # 엑셀에서 한글이 깨지지 않도록 BOM
    return StreamingResponse(iter([data]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": "attachment; filename=portfolio.csv"})
