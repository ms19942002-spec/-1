from app import analyzer, evaluator, ingest


def test_discover_messy_folder(messy):
    found = {c.path.relative_to(messy).as_posix(): c.hint for c in ingest.discover(messy)}
    assert found == {
        "영업팀/실적대시보드": "project",
        "기획팀/월간매출_최종_v2": "project",
        "기획팀/회의자료": "static-site",
        "개인/홍길동/재고현황.html": "html-file",
    }


def test_analyze_fastapi_project(messy):
    a = analyzer.analyze(messy / "영업팀/실적대시보드")
    assert a["kind"] == "fastapi"
    assert {e["path"] for e in a["endpoints"]} == {"/api/sales/monthly", "/api/sales/region"}
    assert "sales_orders" in a["tables"] and "customers" in a["tables"]
    assert "MySQL" in a["db_drivers"]
    assert a["has_requirements"] and not a["has_readme"]
    assert [s["line"] for s in a["secrets"]] == [5]
    assert not any("node_modules" in p["file"] for p in a["pages"])


def test_analyze_single_html(messy):
    a = analyzer.analyze(messy / "개인/홍길동/재고현황.html")
    assert a["kind"] == "static-html"
    assert a["pages"][0]["title"] == "재고 현황"
    assert a["external_script_hosts"] == ["cdn.example.net"]


def test_secret_placeholders_ignored(tmp_path):
    (tmp_path / "a.py").write_text('password = os.getenv("PW")\napi_key = "your-key-here"\n')
    (tmp_path / ".env").write_text("DB_PASSWORD=realvalue123\nDEBUG=true\n")
    found = analyzer.analyze(tmp_path)["secrets"]
    assert [(s["file"], s["line"]) for s in found] == [(".env", 1)]


def test_verdict_quadrants():
    assert evaluator.verdict_for(80, 80) == "Invest"
    assert evaluator.verdict_for(30, 80) == "Migrate"
    assert evaluator.verdict_for(80, 30) == "Tolerate"
    assert evaluator.verdict_for(30, 30) == "Eliminate"


def test_business_value_prefers_human_input(messy):
    heur = evaluator.heuristic_scores(analyzer.analyze(messy / "영업팀/실적대시보드"))
    llm = {"scores": {"business_value": 20}, "maturity": 3}
    low = evaluator.finalize({}, heur, llm)
    high = evaluator.finalize({"importance": 5, "monthly_users": 200}, heur, llm)
    assert low["business"] == 20 and high["business"] > 60
    assert low["maturity_label"] == "파일럿"
