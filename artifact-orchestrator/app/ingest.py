"""산출물 수집: 뒤섞인 폴더 자동 탐색, 업로드(zip/html), 사내 Git, 사내 URL."""
import io
import re
import shutil
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path

import httpx

from . import config

SKIP_DIRS = {".git", ".svn", ".hg", "node_modules", "__pycache__", ".venv", "venv", "env",
             ".idea", ".vscode", "dist", "build", ".mypy_cache", ".pytest_cache", "site-packages"}
PROJECT_MARKERS = {"requirements.txt", "pyproject.toml", "setup.py", "Pipfile", "package.json",
                   "Dockerfile", "docker-compose.yml", "docker-compose.yaml", "manage.py", ".git"}
WEB_APP_RE = re.compile(r"\b(FastAPI|Flask|Starlette)\s*\(")
HTML_EXTS = {".html", ".htm"}
MAX_DEPTH = 10


class IngestError(Exception):
    pass


@dataclass
class Candidate:
    name: str
    path: Path
    hint: str  # project | static-site | html-file


def check_allowed(path: Path) -> Path:
    """AO_ALLOWED_ROOTS 가 지정된 경우 그 하위 경로만 허용한다."""
    p = path.expanduser().resolve()
    roots = config.settings.allowed_roots
    if roots and not any(p == r or r in p.parents for r in roots):
        raise IngestError(f"허용되지 않은 경로입니다: {p} (AO_ALLOWED_ROOTS 확인)")
    if not p.exists():
        raise IngestError(f"경로가 존재하지 않습니다: {p}")
    return p


def _has_web_app_code(d: Path, files: list[Path]) -> bool:
    for f in [f for f in files if f.suffix == ".py"][:30]:
        try:
            if WEB_APP_RE.search(f.read_text(encoding="utf-8", errors="ignore")[:200_000]):
                return True
        except OSError:
            continue
    return False


def discover(root: Path) -> list[Candidate]:
    """폴더 트리를 훑어 '하나의 산출물'로 볼 수 있는 단위를 찾는다.

    - 프로젝트 표식(requirements.txt, package.json, .git 등)이나 FastAPI/Flask 코드가 있는 폴더 → 프로젝트 1개
    - index.html 이 있는 폴더 → 정적 사이트 1개
    - 그 외 폴더에 흩어진 .html 파일 → 파일 하나하나가 산출물
    """
    root = check_allowed(root)
    if root.is_file():
        return [Candidate(root.stem, root, "html-file")] if root.suffix.lower() in HTML_EXTS else []

    found: list[Candidate] = []

    def walk(d: Path, depth: int) -> None:
        if depth > MAX_DEPTH:
            return
        try:
            entries = sorted(d.iterdir())
        except OSError:
            return
        files = [e for e in entries if e.is_file()]
        names = {e.name for e in entries}
        if names & PROJECT_MARKERS or _has_web_app_code(d, files):
            found.append(Candidate(d.name, d, "project"))
            return
        if "index.html" in names or "index.htm" in names:
            found.append(Candidate(d.name, d, "static-site"))
            return
        for f in files:
            if f.suffix.lower() in HTML_EXTS:
                found.append(Candidate(f"{d.name}/{f.stem}" if d != root else f.stem, f, "html-file"))
        for sub in entries:
            if sub.is_dir() and sub.name not in SKIP_DIRS and not sub.name.startswith("."):
                walk(sub, depth + 1)

    walk(root, 0)
    return found


def _new_workspace(prefix: str) -> Path:
    ws = config.settings.workspace_dir
    ws.mkdir(parents=True, exist_ok=True)
    i = 1
    while (ws / f"{prefix}-{i}").exists():
        i += 1
    d = ws / f"{prefix}-{i}"
    d.mkdir()
    return d


def save_upload(filename: str, content: bytes) -> Path:
    """zip 은 안전하게 압축 해제, html 은 그대로 저장."""
    safe = Path(filename).name or "upload"
    dest = _new_workspace("upload")
    if safe.lower().endswith(".zip"):
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as z:
                for m in z.infolist():
                    target = (dest / m.filename).resolve()
                    if dest.resolve() not in target.parents and target != dest.resolve():
                        raise IngestError(f"zip 내부 경로가 올바르지 않습니다: {m.filename}")
                z.extractall(dest)
        except zipfile.BadZipFile as e:
            shutil.rmtree(dest, ignore_errors=True)
            raise IngestError("올바른 zip 파일이 아닙니다") from e
        # 최상위 폴더 하나로만 감싸진 zip 이면 그 안을 분석 대상으로
        children = [c for c in dest.iterdir() if not c.name.startswith("__MACOSX")]
        return children[0] if len(children) == 1 and children[0].is_dir() else dest
    if Path(safe).suffix.lower() not in HTML_EXTS:
        shutil.rmtree(dest, ignore_errors=True)
        raise IngestError("zip 또는 html 파일만 업로드할 수 있습니다")
    (dest / safe).write_bytes(content)
    return dest / safe


def clone_git(url: str) -> Path:
    """사내 Git 서버에서 얕은 복제. 외부 전송은 없고 내려받기만 한다."""
    if url.startswith("-") or not re.match(r"^(https?://|ssh://|git@)", url):
        raise IngestError("http(s)://, ssh://, git@ 형식의 Git 주소만 허용합니다")
    dest = _new_workspace("git")
    try:
        subprocess.run(["git", "clone", "--depth", "1", "--", url, str(dest)],
                       check=True, capture_output=True, timeout=300)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        shutil.rmtree(dest, ignore_errors=True)
        detail = getattr(e, "stderr", b"") or b""
        raise IngestError(f"git clone 실패: {detail.decode(errors='ignore')[:300] or e}") from e
    return dest


def fetch_url(url: str) -> Path:
    """사내 웹페이지 HTML 을 내려받아 저장한다 (정적 HTML 산출물 등록용)."""
    if not re.match(r"^https?://", url):
        raise IngestError("http(s):// 주소만 허용합니다")
    try:
        r = httpx.get(url, timeout=30, follow_redirects=True)
        r.raise_for_status()
    except httpx.HTTPError as e:
        raise IngestError(f"URL 요청 실패: {e}") from e
    dest = _new_workspace("url")
    (dest / "index.html").write_text(r.text, encoding="utf-8")
    return dest
