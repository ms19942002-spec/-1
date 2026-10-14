import importlib
import textwrap
from pathlib import Path

import pytest


def write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(text), encoding="utf-8")


@pytest.fixture
def messy(tmp_path: Path) -> Path:
    """사내 공유폴더처럼 뒤섞인 구조."""
    root = tmp_path / "share"
    write(root / "영업팀/실적대시보드/main.py", """
        from fastapi import FastAPI
        import pymysql
        app = FastAPI()
        DB_PASSWORD = "Sup3rSecret!"

        @app.get("/api/sales/monthly")
        def monthly():
            sql = "SELECT region, amount FROM sales_orders JOIN customers c ON 1=1"
            return []

        @app.get("/api/sales/region")
        def region():
            return "SELECT * FROM sales_orders"
        """)
    write(root / "영업팀/실적대시보드/requirements.txt", "fastapi\npymysql\n")
    write(root / "영업팀/실적대시보드/node_modules/junk/index.html", "<title>무시</title>")
    write(root / "기획팀/월간매출_최종_v2/app.py", """
        from fastapi import FastAPI
        app = FastAPI()

        @app.get("/sales/monthly/report")
        def report():
            return "select amount from sales_orders join customers on 1=1"
        """)
    write(root / "기획팀/회의자료/index.html", "<html><title>회의 대시보드</title><h1>주간 회의</h1></html>")
    write(root / "개인/홍길동/재고현황.html",
          "<html><title>재고 현황</title><form></form><script src='https://cdn.example.net/x.js'></script></html>")
    write(root / "개인/홍길동/메모.txt", "산출물 아님")
    return root


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AO_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AO_SYNC_EVAL", "true")
    monkeypatch.setenv("AO_LLM_BASE_URL", "http://127.0.0.1:9")  # 연결 불가 주소
    from app import config
    config.settings = config.load()
    from app import main
    importlib.reload(main)
    return main
