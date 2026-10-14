"""산출물 간 유사도로 통합 후보 그룹을 찾는다 (외부 모델 없이 로컬 계산)."""
import re
from itertools import combinations

from .analyzer import tokenize

GENERIC_PATH_PARTS = {"api", "v1", "v2", "v3", "health", "docs", "static", "login", "logout", ""}


def features(analysis: dict) -> dict[str, float]:
    """같은 DB 테이블을 쓰는 것이 가장 강한 통합 신호이므로 가중치를 높게 준다."""
    f: dict[str, float] = {}
    for t in analysis.get("tables", []):
        f[f"테이블:{t.split('.')[-1]}"] = 3.0
    for e in analysis.get("endpoints", []):
        for part in re.split(r"[/_\-]", e["path"].lower()):
            if part not in GENERIC_PATH_PARTS and not part.startswith("{") and not part.isdigit():
                f[f"API:{part}"] = max(f.get(f"API:{part}", 0), 1.5)
    for api in analysis.get("called_apis", []):
        for tok in tokenize(api.replace("/", " ")):
            f[f"호출API:{tok}"] = 1.5
    for i, k in enumerate(analysis.get("keywords", [])[:30]):
        f.setdefault(f"키워드:{k}", 1.0 - i * 0.02)
    return f


def weighted_jaccard(a: dict[str, float], b: dict[str, float]) -> float:
    keys = a.keys() | b.keys()
    if not keys:
        return 0.0
    num = sum(min(a.get(k, 0), b.get(k, 0)) for k in keys)
    den = sum(max(a.get(k, 0), b.get(k, 0)) for k in keys)
    return num / den if den else 0.0


def find_groups(artifacts: list[dict], threshold: float) -> list[dict]:
    items = [(a["id"], features(a["analysis"])) for a in artifacts if a.get("analysis")]
    parent = {i: i for i, _ in items}

    def root(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    pairs = []
    for (ia, fa), (ib, fb) in combinations(items, 2):
        score = weighted_jaccard(fa, fb)
        if score >= threshold:
            shared = sorted(fa.keys() & fb.keys(), key=lambda k: -min(fa[k], fb[k]))
            pairs.append({"a": ia, "b": ib, "score": round(score, 3), "shared": shared[:12]})
            parent[root(ia)] = root(ib)

    groups: dict[int, dict] = {}
    for p in pairs:
        g = groups.setdefault(root(p["a"]), {"members": set(), "pairs": [], "shared": {}})
        g["members"].update((p["a"], p["b"]))
        g["pairs"].append(p)
        for s in p["shared"]:
            g["shared"][s] = g["shared"].get(s, 0) + 1
    out = []
    for g in groups.values():
        members = sorted(g["members"])
        out.append({
            "key": ",".join(map(str, members)),
            "members": members,
            "pairs": sorted(g["pairs"], key=lambda p: -p["score"]),
            "max_score": max(p["score"] for p in g["pairs"]),
            "shared": [k for k, _ in sorted(g["shared"].items(), key=lambda kv: -kv[1])][:15],
        })
    return sorted(out, key=lambda g: (-len(g["members"]), -g["max_score"]))
