"""모델 호출은 여기 한 곳에서만 합니다(docs/ai/README.md 5절). 지금은 `claude -p`, 나중에 API 키로 바꿀 자리입니다.

- 도구 없이(`--tools ""`) 부르고, 답은 `--json-schema`로 받습니다. 도구는 코드가 부릅니다(E04 H1).
- 사용자 설정과 프로젝트 지시 파일이 끼지 않게 빈 작업 폴더에서 `--setting-sources ""`로 부릅니다.
- 호출마다 비용(`total_cost_usd`)과 걸린 시간을 장부에 적고, 누적 비용이 상한(BUDGET_USD)에 닿으면 더 부르지 않습니다.
- 장부와 실행 기록의 자리는 데이터 종류(kind)로 정합니다(docs/ai/F-screen.md 2절 바꿀 것 7). 가짜 세계("fake", E04)는
  eval/e04_agent/runs/(git 추적), 실제 데이터("real", F 화면, E02)는 data/generated/f_screen/(git 제외)입니다.
  실제 데이터의 도구 결과와 모델 글에는 실제 시세가 섞일 수 있어서입니다. kind는 기본값이 없어 부르는 쪽이 꼭 정하고,
  실제 데이터를 data/generated/ 밖에 쓰려 하면 guard()가 멈춥니다. 상한은 두 장부를 더해 봅니다(6절 8번).
"""
import json
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "eval" / "e04_agent" / "runs" / "ledger.jsonl"
REAL_LEDGER = ROOT / "data" / "generated" / "f_screen" / "ledger.jsonl"
PLACES = {"fake": (LEDGER, ROOT / "eval" / "e04_agent" / "runs"),
          "real": (REAL_LEDGER, ROOT / "data" / "generated" / "f_screen" / "runs")}
SAFE = ROOT / "data" / "generated"
MODEL = "claude-sonnet-5-5"
BUDGET_USD = 25.0
MAX_CALLS = 1500
TIMEOUT_S = 180
_WORKDIR = tempfile.mkdtemp(prefix="e04-")
_lock = threading.Lock()


class BudgetExceeded(RuntimeError):
    pass


def spent():
    """두 장부를 더한 누적 비용과 호출 수."""
    rows = []
    for path in (LEDGER, REAL_LEDGER):
        if path.exists():
            rows += [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    return sum(r.get("cost_usd") or 0 for r in rows), len(rows)


def guard(path, kind):
    """실제 데이터("real")의 기록이 git 제외 폴더(data/generated/) 밖으로 가면 멈춥니다."""
    if kind not in PLACES:
        raise ValueError(f"kind는 fake나 real입니다: {kind}")
    if kind == "real" and not Path(path).resolve().is_relative_to(SAFE.resolve()):
        raise RuntimeError(f"실제 데이터의 기록은 data/generated/ 아래에만 씁니다: {path}")
    return Path(path)


def ledger_for(kind):
    return guard(PLACES[kind][0] if kind in PLACES else None, kind)


def runs_dir(kind):
    """실행 기록 폴더. 6단계의 묻고 답하기와 E02 실행기는 Toolset의 data.kind로 이 폴더를 고릅니다."""
    return guard(PLACES[kind][1] if kind in PLACES else None, kind)


def call(prompt, system, schema=None, model=MODEL, tag="", *, kind):
    """모델을 한 번 부릅니다. 돌려주는 것: {"output": 구조화된 답(dict) 또는 글, "cost_usd", "seconds", "usage", "error"}.
    kind("fake" 또는 "real")가 장부 자리를 정합니다. 기본값은 없습니다."""
    ledger = ledger_for(kind)
    cost, n = spent()
    if cost >= BUDGET_USD or n >= MAX_CALLS:
        raise BudgetExceeded(f"상한에 닿음: ${cost:.2f}, {n}번")
    cmd = ["claude", "-p", "--model", model, "--tools", "", "--output-format", "json",
           "--no-session-persistence", "--setting-sources", "", "--strict-mcp-config",
           "--disable-slash-commands", "--system-prompt", system,
           "--max-budget-usd", f"{max(0.05, BUDGET_USD - cost):.2f}"]
    if schema is not None:
        cmd += ["--json-schema", json.dumps(schema, ensure_ascii=False)]
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_CODE") and k != "CLAUDECODE"}
    t0 = time.perf_counter()
    try:
        p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=TIMEOUT_S,
                           cwd=_WORKDIR, env=env)
        seconds = time.perf_counter() - t0
        raw = p.stdout.strip()
        res = json.loads(raw) if raw.startswith("{") else {"is_error": True, "result": (raw or p.stderr)[-500:]}
    except subprocess.TimeoutExpired:
        seconds, res = time.perf_counter() - t0, {"is_error": True, "result": f"{TIMEOUT_S}초 넘음"}
    out = res.get("structured_output") if schema is not None else res.get("result")
    error = res.get("result") if res.get("is_error") or (schema is not None and out is None) else None
    if isinstance(error, str):
        error = error[-500:]
    row = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "tag": tag, "model": model,
           "cost_usd": res.get("total_cost_usd"), "seconds": round(seconds, 2),
           "usage": {k: res.get("usage", {}).get(k) for k in
                     ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")},
           "error": error}
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with _lock, ledger.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return {"output": out, **{k: row[k] for k in ("cost_usd", "seconds", "usage", "error")}}


def boot_seconds(tries=3, kind="fake"):
    """빈 호출로 CLI 기동과 왕복 시간을 잽니다. 지연에서 뺄 값(E04 3절). 데이터가 없는 호출이라 장부만 고릅니다."""
    runs = [call("ok", "Reply with the single word ok.", tag="boot", kind=kind) for _ in range(tries)]
    return sorted(r["seconds"] for r in runs)[len(runs) // 2], runs
