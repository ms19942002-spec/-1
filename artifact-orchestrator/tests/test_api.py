from fastapi.testclient import TestClient


def test_scan_evaluate_and_group(app_env, messy, monkeypatch):
    main = app_env
    calls = []

    def fake_chat(system, user):
        calls.append(system)
        if "통합" in system[:40]:
            return {"should_merge": True, "overlap": "sales_orders 테이블", "target_name": "영업 분석 포털",
                    "steps": ["공통 API 분리"], "retire": ["월간매출_최종_v2"], "risks": []}
        return {"summary": "요약", "maturity": 2, "verdict": "Migrate",
                "scores": {"completeness": 50, "code_quality": 50, "maintainability": 40,
                           "security": 30, "business_value": 70},
                "next_steps": ["비밀번호 환경변수로 분리"]}

    monkeypatch.setattr(main.llm, "chat_json", fake_chat)
    with TestClient(main.app) as c:
        r = c.post("/scan", data={"root": str(messy)}, follow_redirects=False)
        assert r.status_code == 303
        arts = c.get("/api/artifacts").json()
        assert len(arts) == 4 and all(a["status"] == "done" for a in arts)
        assert all(a["evaluation"]["llm_status"] == "ok" for a in arts)

        # 다시 스캔해도 중복 등록되지 않는다
        c.post("/scan", data={"root": str(messy)})
        assert len(c.get("/api/artifacts").json()) == 4

        groups = c.get("/api/groups").json()
        by_name = {a["id"]: a["name"] for a in arts}
        merged = [{by_name[i] for i in g["members"]} for g in groups]
        assert {"실적대시보드", "월간매출_최종_v2"} in merged
        assert any("테이블:sales_orders" in g["shared"] for g in groups)

        key = next(g["key"] for g in groups if "테이블:sales_orders" in g["shared"])
        c.post("/groups/plan", data={"key": key})
        assert "영업 분석 포털" in c.get("/groups").text

        aid = arts[0]["id"]
        c.post(f"/artifacts/{aid}/meta", data={"name": "실적", "importance": "5", "monthly_users": "300",
                                               "decision": "Invest"})
        a = c.get(f"/api/artifacts/{aid}").json()
        assert a["decision"] == "Invest" and a["evaluation"]["final"]["business"] > 70

        for path in ["/", f"/artifacts/{aid}", "/groups", "/export.csv"]:
            assert c.get(path).status_code == 200


def test_works_without_llm(app_env, messy):
    main = app_env
    with TestClient(main.app) as c:
        c.post("/scan", data={"root": str(messy / "개인")})
        arts = c.get("/api/artifacts").json()
        assert len(arts) == 1
        ev = arts[0]["evaluation"]
        assert arts[0]["status"] == "done" and ev["llm"] is None and "LLM 호출 실패" in ev["llm_status"]
        assert ev["final"]["verdict"] in {"Invest", "Tolerate", "Migrate", "Eliminate"}
        assert "규칙 기반" in c.get(f"/artifacts/{arts[0]['id']}").text


def test_upload_zip_rejects_path_traversal(app_env):
    import io, zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("../evil.py", "x")
    with TestClient(app_env.app) as c:
        r = c.post("/artifacts", data={"source_type": "upload"},
                   files={"file": ("a.zip", buf.getvalue(), "application/zip")})
        assert "올바르지 않습니다" in r.text
        assert c.get("/api/artifacts").json() == []
