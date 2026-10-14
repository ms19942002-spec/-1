"""정적 분석: LLM 없이 코드/HTML 에서 객관적인 사실을 뽑아낸다."""
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

from .ingest import SKIP_DIRS

TEXT_EXTS = {
    ".py": "Python", ".js": "JavaScript", ".ts": "TypeScript", ".jsx": "JavaScript", ".tsx": "TypeScript",
    ".vue": "Vue", ".html": "HTML", ".htm": "HTML", ".css": "CSS", ".scss": "CSS", ".sql": "SQL",
    ".java": "Java", ".kt": "Kotlin", ".go": "Go", ".sh": "Shell", ".ipynb": "Notebook",
    ".md": "Markdown", ".txt": "Text", ".json": "JSON", ".yml": "YAML", ".yaml": "YAML", ".toml": "TOML",
    ".cfg": "Config", ".ini": "Config", ".env": "Config",
}
CODE_LANGS = {"Python", "JavaScript", "TypeScript", "Vue", "HTML", "CSS", "SQL", "Java", "Kotlin", "Go", "Shell"}
MAX_FILES = 5000
MAX_FILE_BYTES = 1_000_000

ENDPOINT_RE = re.compile(
    r"@\w+\.(get|post|put|delete|patch|route|api_route)\(\s*[rf]?['\"]([^'\"]*)['\"]", re.I)
EXPRESS_RE = re.compile(r"\b(?:app|router)\.(get|post|put|delete|patch)\(\s*['\"`]([^'\"`]+)['\"`]")
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
H1_RE = re.compile(r"<h[12][^>]*>(.*?)</h[12]>", re.I | re.S)
TAG_RE = re.compile(r"<[^>]+>")
EXT_SCRIPT_RE = re.compile(r"<script[^>]+src=['\"](https?:)?//([^/'\"]+)", re.I)
FETCH_RE = re.compile(r"(?:fetch|axios\.\w+|\$\.(?:ajax|get|post))\(\s*['\"`]([^'\"`]+)['\"`]")
TABLE_RES = [
    re.compile(r"\bselect\b[\s\S]{0,300}?\bfrom\s+([A-Za-z_][\w.]*)", re.I),
    re.compile(r"\bjoin\s+([A-Za-z_][\w.]*)\s", re.I),
    re.compile(r"\binsert\s+into\s+([A-Za-z_][\w.]*)", re.I),
    re.compile(r"\bupdate\s+([A-Za-z_][\w.]*)\s+set\b", re.I),
    re.compile(r"__tablename__\s*=\s*['\"](\w+)['\"]"),
    re.compile(r"read_sql(?:_table)?\(\s*['\"]([A-Za-z_][\w.]*)['\"]"),
]
NOT_TABLES = {"import", "the", "a", "an", "this", "that", "dual", "select", "where", "table", "values"}
DB_DRIVERS = {"sqlalchemy": "SQLAlchemy", "pymysql": "MySQL", "mysql.connector": "MySQL",
              "psycopg": "PostgreSQL", "cx_oracle": "Oracle", "oracledb": "Oracle", "pyodbc": "ODBC/MSSQL",
              "pymssql": "MSSQL", "sqlite3": "SQLite", "pymongo": "MongoDB", "redis": "Redis",
              "pandas.read_excel": "Excel", "openpyxl": "Excel"}
AUTH_RE = re.compile(r"OAuth2|HTTPBasic|HTTPBearer|jwt|login_required|LoginManager|authenticate|sso|ldap", re.I)
SECRET_RES = [
    ("하드코딩된 비밀번호/키", re.compile(
        r"(?i)\b\w*(password|passwd|pwd|secret|api_?key|access_?key|token)\w*['\"]?\s*[:=]\s*['\"][^'\"\s]{4,}['\"]")),
    ("DB 접속 문자열에 비밀번호 포함", re.compile(
        r"(?i)\b(mysql|postgres(?:ql)?|oracle|mssql|mongodb)[\w+]*://[^:\s/]+:[^@\s]+@")),
    ("환경파일에 비밀번호/키 평문 저장", re.compile(
        r"(?im)^\s*\w*(?:PASSWORD|PASSWD|SECRET|API_?KEY|TOKEN)\w*\s*=\s*[^\s'\"#]{4,}\s*$")),
    ("AWS 액세스 키", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("개인 키 파일 내용", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
]
PLACEHOLDER_RE = re.compile(r"(?i)changeme|example|your[_-]|xxx|\*\*\*|<.*>|\{.*\}|os\.environ|getenv")
TOKEN_RE = re.compile(r"[A-Za-z][a-z]+|[A-Z]{2,}(?=[A-Z][a-z]|\b)|[가-힣]{2,}")
STOPWORDS = {
    "the", "and", "for", "with", "from", "this", "that", "api", "app", "get", "post", "put", "delete",
    "html", "index", "main", "page", "data", "list", "info", "item", "items", "true", "false", "none",
    "return", "def", "class", "import", "self", "http", "https", "www", "com", "static", "templates",
    "test", "tests", "src", "lib", "utils", "util", "readme", "json", "id", "ids", "new", "old", "copy",
    "final", "version", "ver", "temp", "tmp", "작업", "파일", "최종", "수정", "복사본",
}


def _iter_files(root: Path):
    if root.is_file():
        yield root
        return
    count = 0
    for p in sorted(root.rglob("*")):
        if count >= MAX_FILES:
            return
        if any(part in SKIP_DIRS or part.startswith(".") and part not in {".env"}
               for part in p.relative_to(root).parts[:-1]):
            continue
        if p.is_file():
            count += 1
            yield p


def _read(p: Path) -> str | None:
    try:
        if p.stat().st_size > MAX_FILE_BYTES:
            return None
        text = p.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    if p.suffix == ".ipynb":
        try:
            nb = json.loads(text)
            text = "\n".join("".join(c.get("source", [])) for c in nb.get("cells", []))
        except (ValueError, AttributeError):
            pass
    return text


def tokenize(text: str) -> list[str]:
    out = []
    for t in TOKEN_RE.findall(text):
        t = t.lower()
        if len(t) >= 3 or re.match(r"[가-힣]", t):
            if t not in STOPWORDS:
                out.append(t)
    return out


def _clean(html_fragment: str) -> str:
    return re.sub(r"\s+", " ", TAG_RE.sub("", html_fragment)).strip()


def analyze(path: Path) -> dict:
    root = Path(path)
    loc: Counter = Counter()
    files_by_lang: Counter = Counter()
    endpoints: list[dict] = []
    pages: list[dict] = []
    tables: Counter = Counter()
    drivers: set[str] = set()
    deps: set[str] = set()
    secrets: list[dict] = []
    keywords: Counter = Counter()
    called_apis: set[str] = set()
    external_hosts: set[str] = set()
    big_files: list[dict] = []
    todo = 0
    auth = False
    logging_used = False
    has_tests = False
    framework_hits: Counter = Counter()
    file_count = 0
    total_bytes = 0
    latest_mtime = 0.0

    for f in _iter_files(root):
        file_count += 1
        try:
            st = f.stat()
        except OSError:
            continue
        total_bytes += st.st_size
        latest_mtime = max(latest_mtime, st.st_mtime)
        rel = f.name if root.is_file() else str(f.relative_to(root))
        lname = f.name.lower()
        if lname.startswith("test_") or lname.endswith("_test.py") or "/tests/" in f"/{rel}" or ".spec." in lname:
            has_tests = True
        lang = TEXT_EXTS.get(f.suffix.lower()) or ("Config" if lname.startswith(".env") else None)
        if lang is None:
            continue
        text = _read(f)
        if text is None:
            continue
        lines = text.count("\n") + 1
        files_by_lang[lang] += 1
        if lang in CODE_LANGS:
            loc[lang] += lines
            if lines > 800:
                big_files.append({"file": rel, "lines": lines})
        todo += len(re.findall(r"\b(TODO|FIXME|XXX)\b", text))

        if lang == "Python":
            for m in ENDPOINT_RE.finditer(text):
                endpoints.append({"method": m.group(1).upper(), "path": m.group(2) or "/", "file": rel})
            for fw in ("FastAPI", "Flask", "Starlette", "streamlit", "gradio", "django", "dash"):
                if re.search(rf"\b{fw}\b", text):
                    framework_hits[fw.lower()] += 1
            low = text.lower()
            for key, name in DB_DRIVERS.items():
                if re.search(rf"\bimport {re.escape(key)}|\bfrom {re.escape(key)}\b|\b{re.escape(key)}\(", low):
                    drivers.add(name)
            logging_used |= "import logging" in text or "logger." in text
        if lang in {"JavaScript", "TypeScript"}:
            for m in EXPRESS_RE.finditer(text):
                endpoints.append({"method": m.group(1).upper(), "path": m.group(2), "file": rel})
        if lang in {"Python", "SQL", "JavaScript", "TypeScript", "Java", "Notebook"}:
            for rx in TABLE_RES:
                for m in rx.finditer(text):
                    t = m.group(1).lower()
                    if t not in NOT_TABLES and not t.startswith(("__", "self.")):
                        tables[t] += 1
        if lang in {"Python", "JavaScript", "TypeScript", "HTML", "Vue"}:
            auth |= bool(AUTH_RE.search(text))
            for m in FETCH_RE.finditer(text):
                called_apis.add(m.group(1)[:120])
        if lang == "HTML":
            title = _clean(TITLE_RE.search(text).group(1)) if TITLE_RE.search(text) else ""
            heads = [_clean(h) for h in H1_RE.findall(text)][:5]
            for m in EXT_SCRIPT_RE.finditer(text):
                external_hosts.add(m.group(2))
            pages.append({"file": rel, "title": title, "headings": heads,
                          "forms": len(re.findall(r"<form\b", text, re.I)),
                          "scripts": len(re.findall(r"<script\b", text, re.I)),
                          "tables": len(re.findall(r"<table\b", text, re.I)),
                          "kb": round(len(text.encode()) / 1024, 1)})
            keywords.update(tokenize(title + " " + " ".join(heads)) * 3)
            keywords.update(tokenize(_clean(text))[:400])
        if lname == "requirements.txt":
            deps.update(re.split(r"[<>=~!\[;\s]", ln.strip())[0].lower()
                        for ln in text.splitlines() if ln.strip() and not ln.startswith(("#", "-")))
        if lname == "package.json":
            try:
                pkg = json.loads(text)
                deps.update((pkg.get("dependencies") or {}).keys())
            except ValueError:
                pass
        if lang not in {"Markdown", "Text"}:
            for label, rx in SECRET_RES:
                if label.startswith("환경파일") and lang != "Config":
                    continue
                for m in rx.finditer(text):
                    if PLACEHOLDER_RE.search(m.group(0)):
                        continue
                    line_no = text.count("\n", 0, m.start()) + 1
                    secrets.append({"type": label, "file": rel, "line": line_no})
        if lang == "Markdown" and lname.startswith("readme"):
            keywords.update(tokenize(" ".join(re.findall(r"^#+\s*(.+)$", text, re.M))) * 2)
        if lang == "Python":
            keywords.update(tokenize(" ".join(re.findall(r"def (\w+)", text))))

    for e in endpoints:
        keywords.update(tokenize(e["path"].replace("/", " ").replace("_", " ")) * 2)
    keywords.update(tokenize(" ".join(tables)))

    names = set()
    if root.is_dir():
        names = {p.name.lower() for p in root.iterdir()}
    kind = _detect_kind(framework_hits, files_by_lang, names, endpoints)

    return {
        "kind": kind,
        "frameworks": sorted(framework_hits),
        "file_count": file_count,
        "total_kb": round(total_bytes / 1024, 1),
        "loc": dict(loc.most_common()),
        "total_loc": sum(loc.values()),
        "endpoints": endpoints[:200],
        "pages": pages[:100],
        "tables": [t for t, _ in tables.most_common(40)],
        "db_drivers": sorted(drivers),
        "dependencies": sorted(d for d in deps if d)[:80],
        "called_apis": sorted(called_apis)[:50],
        "external_script_hosts": sorted(external_hosts),
        "has_readme": any(n.startswith("readme") for n in names),
        "has_requirements": bool(names & {"requirements.txt", "pyproject.toml", "package.json", "pipfile"}),
        "has_dockerfile": bool(names & {"dockerfile", "docker-compose.yml", "docker-compose.yaml"}),
        "has_env_example": bool(names & {".env.example", ".env.sample", "env.example"}),
        "has_tests": has_tests,
        "has_auth": auth,
        "uses_logging": logging_used,
        "todo_count": todo,
        "big_files": big_files[:20],
        "secrets": secrets[:50],
        "last_modified": datetime.fromtimestamp(latest_mtime).isoformat(timespec="seconds") if latest_mtime else None,
        "keywords": [k for k, _ in keywords.most_common(40)],
    }


def _detect_kind(fw: Counter, langs: Counter, names: set[str], endpoints: list) -> str:
    if "fastapi" in fw:
        return "fastapi"
    if "flask" in fw:
        return "flask"
    if "django" in fw or "manage.py" in names:
        return "django"
    if "streamlit" in fw or "gradio" in fw or "dash" in fw:
        return "python-ui"
    if "package.json" in names or (endpoints and langs.get("JavaScript")):
        return "node"
    if langs.get("Python") or langs.get("Notebook"):
        return "python-script"
    if langs.get("HTML"):
        return "static-html"
    return "unknown"


def key_files(path: Path, limit_chars: int) -> list[tuple[str, str]]:
    """LLM 에게 보여줄 핵심 파일 발췌 (README → 진입점 → 큰 코드 파일 순)."""
    root = Path(path)
    if root.is_file():
        text = _read(root) or ""
        return [(root.name, text[:limit_chars])]
    files = [f for f in _iter_files(root) if f.suffix.lower() in TEXT_EXTS]

    def priority(f: Path) -> tuple:
        n = f.name.lower()
        depth = len(f.relative_to(root).parts)
        if n.startswith("readme"):
            return (0, depth)
        if n in {"main.py", "app.py", "server.py", "api.py", "index.html", "routes.py", "views.py", "manage.py"}:
            return (1, depth)
        if n in {"requirements.txt", "package.json", "pyproject.toml", "dockerfile"}:
            return (2, depth)
        if f.suffix.lower() in {".py", ".js", ".ts", ".html", ".sql", ".vue"}:
            return (3, depth, -f.stat().st_size)
        return (9, depth)

    out, used = [], 0
    for f in sorted(files, key=priority):
        if priority(f)[0] == 9 or used >= limit_chars:
            break
        text = _read(f)
        if not text:
            continue
        budget = min(len(text), max(1500, (limit_chars - used) // 2), limit_chars - used)
        out.append((str(f.relative_to(root)), text[:budget]))
        used += budget
    return out
