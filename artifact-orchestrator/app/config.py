"""환경변수 기반 설정. 모든 값은 AO_ 접두사를 사용한다."""
import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _bool(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    llm_enabled: bool
    llm_base_url: str
    llm_model: str
    llm_api_key: str
    llm_timeout: float
    max_context_chars: int
    similarity_threshold: float
    sync_eval: bool
    allowed_roots: list[Path] = field(default_factory=list)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "orchestrator.db"

    @property
    def workspace_dir(self) -> Path:
        return self.data_dir / "workspace"


def load() -> Settings:
    roots = [Path(p).resolve() for p in os.getenv("AO_ALLOWED_ROOTS", "").split(os.pathsep) if p.strip()]
    return Settings(
        data_dir=Path(os.getenv("AO_DATA_DIR", str(BASE_DIR / "data"))),
        llm_enabled=_bool("AO_LLM_ENABLED", "true"),
        # Ollama / vLLM / LM Studio 등 OpenAI 호환 엔드포인트 (사내망 전용)
        llm_base_url=os.getenv("AO_LLM_BASE_URL", "http://localhost:11434/v1").rstrip("/"),
        llm_model=os.getenv("AO_LLM_MODEL", "qwen2.5-coder:14b"),
        llm_api_key=os.getenv("AO_LLM_API_KEY", "local"),
        llm_timeout=float(os.getenv("AO_LLM_TIMEOUT", "300")),
        max_context_chars=int(os.getenv("AO_MAX_CONTEXT_CHARS", "16000")),
        similarity_threshold=float(os.getenv("AO_SIMILARITY_THRESHOLD", "0.3")),
        sync_eval=_bool("AO_SYNC_EVAL", "false"),
        allowed_roots=roots,
    )


settings = load()
