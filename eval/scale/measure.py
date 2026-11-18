#!/usr/bin/env python3
"""규모 시험 측정(docs/reports/2026-11-18_scale-test.md). 결과는 eval/scale/results.json에 이름별로 쌓습니다.

사용(저장소 루트에서, Spark 컨테이너가 떠 있어야 함):
  .venv/bin/python eval/scale/measure.py bronze <이름>        loaders/load_test.py 한 번의 CDC 이벤트가 Bronze에 보이기까지
  .venv/bin/python eval/scale/measure.py wait <이름>          그 이벤트가 Bronze에 모두 보일 때까지 기다림
  .venv/bin/python eval/scale/measure.py files <이름>          Bronze, Silver 테이블의 데이터 파일, 스냅샷 수
  .venv/bin/python eval/scale/measure.py dag <이름> <run_id>   Airflow DAG 한 번의 태스크별 걸린 시간
  .venv/bin/python eval/scale/measure.py mem <이름>            컨테이너별 메모리(docker stats 한 번)
  .venv/bin/python eval/scale/measure.py show                  모은 결과를 표로

Bronze 지연(이벤트 하나마다, 밀리초):
  capture  = Kafka 레코드 시각 - 원천 커밋 시각(Debezium source.ts_ms). Debezium이 WAL을 읽어 Kafka에 쓰기까지
  read     = Flink가 행을 만든 시각(ingested_at) - Kafka 레코드 시각. Flink가 읽기까지
  commit   = 그 행이 든 데이터 파일을 더한 Iceberg 스냅샷의 커밋 시각 - ingested_at. checkpoint를 기다려 커밋하기까지
  total    = 스냅샷 커밋 시각 - 원천 커밋 시각. 원천에 커밋한 변경이 Bronze 테이블을 읽는 쪽에 보이기까지
행과 스냅샷은 Iceberg 메타데이터 열 _file과 entries, snapshots 메타데이터 테이블로 잇습니다.
모든 컨테이너가 같은 VM의 시계를 씁니다. 이벤트는 runs.jsonl에 남은 WAL 범위(lsn_from ~ lsn_to)로 고릅니다.
"""
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from ai.screen_data import _spark_rows  # noqa: E402  내 Mac은 beeline, 컨테이너는 pyhive

RUNS = ROOT / "data" / "generated" / "scale" / "runs.jsonl"
OUT = Path(__file__).with_name("results.json")
BRONZE = ("orders", "executions", "holdings")
SILVER = ("orders", "executions", "holdings")
PCT = "array(0.5, 0.95, 0.99)"


def load():
    return json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}


def save(key, value):
    res = load()
    res[key] = value
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def run_of(name):
    runs = [json.loads(ln) for ln in RUNS.read_text(encoding="utf-8").splitlines() if ln.strip()]
    hit = [r for r in runs if r["name"] == name]
    if not hit:
        sys.exit(f"runs.jsonl에 {name}이 없습니다")
    return hit[-1]


def arr(v):
    """Spark 배열 글자 [1.0,2.0] → 정수 목록."""
    return [round(float(x)) for x in v.strip("[]").split(",")] if v not in (None, "NULL") else None


def bronze(name):
    r = run_of(name)
    out = {"run": r, "tables": {}}
    for t in BRONZE:
        sql = f"""
with x as (
  select _file as f, cast(get_json_object(payload, '$.source.ts_ms') as bigint) as src_ms,
         unix_millis(kafka_ts) as kafka_ms, unix_millis(ingested_at) as ing_ms,
         cast(get_json_object(payload, '$.source.lsn') as bigint) as lsn
  from lake.bronze.{t}
), y as (select * from x where lsn >= {int(r['lsn_from'])} and lsn <= {int(r['lsn_to'])}),
fe as (select data_file.file_path as f, max(snapshot_id) as snapshot_id from lake.bronze.{t}.entries
       where status <> 2 group by data_file.file_path),
s as (select snapshot_id, unix_millis(committed_at) as com_ms from lake.bronze.{t}.snapshots),
z as (select y.*, s.com_ms, s.snapshot_id from y join fe on y.f = fe.f join s on fe.snapshot_id = s.snapshot_id)
select count(*), (select count(*) from y), min(src_ms), max(src_ms), max(com_ms),
  percentile(kafka_ms - src_ms, {PCT}), max(kafka_ms - src_ms),
  percentile(ing_ms - kafka_ms, {PCT}), max(ing_ms - kafka_ms),
  percentile(com_ms - ing_ms, {PCT}), max(com_ms - ing_ms),
  percentile(com_ms - src_ms, {PCT}), max(com_ms - src_ms),
  count(distinct f), count(distinct snapshot_id)
from z"""
        row = _spark_rows(sql)[0]
        n, n_all = int(row[0]), int(row[1])
        if n == 0:
            out["tables"][t] = {"events": 0, "events_in_range": n_all}
            continue
        src0, src1, com1 = int(row[2]), int(row[3]), int(row[4])
        out["tables"][t] = {
            "events": n, "events_in_range": n_all,
            "capture_ms": {"p50_p95_p99": arr(row[5]), "max": int(row[6])},
            "read_ms": {"p50_p95_p99": arr(row[7]), "max": int(row[8])},
            "commit_ms": {"p50_p95_p99": arr(row[9]), "max": int(row[10])},
            "total_ms": {"p50_p95_p99": arr(row[11]), "max": int(row[12])},
            "source_span_s": round((src1 - src0) / 1000, 1),
            "drain_s": round((com1 - src0) / 1000, 1),
            "events_per_s": round(n / max((com1 - src0) / 1000, 0.001), 1),
            "data_files": int(row[13]), "snapshots": int(row[14]),
        }
    save(f"bronze:{name}", out)
    return out


def wait(name, limit=900):
    """한 번의 이벤트가 Bronze 주문, 체결 테이블에 모두 보일 때까지 10초마다 셉니다(지움은 체결 수 = 지운 체결)."""
    import time
    r = run_of(name)
    want = r["trades"]
    t0 = time.time()
    while time.time() - t0 < limit:
        got = [int(_spark_rows(f"select count(*) from lake.bronze.{t} where cast(get_json_object(payload, '$.source.lsn') "
                               f"as bigint) between {int(r['lsn_from'])} and {int(r['lsn_to'])}")[0][0])
               for t in ("orders", "executions")]
        print(f"  {round(time.time() - t0)}초: Bronze 주문 {got[0]}, 체결 {got[1]} / {want}", flush=True)
        if min(got) >= want:
            return True
        time.sleep(10)
    return False


def files(name):
    out = {}
    for layer, tables in (("bronze", BRONZE), ("silver", SILVER)):
        for t in tables:
            full = f"lake.{layer}.{t}"
            f = _spark_rows(f"select count(*), coalesce(sum(file_size_in_bytes), 0), coalesce(sum(record_count), 0) "
                            f"from {full}.files")[0]
            s = _spark_rows(f"select count(*) from {full}.snapshots")[0]
            m = _spark_rows(f"select count(*) from {full}.manifests")[0]
            out[full] = {"data_files": int(f[0]), "bytes": int(f[1]), "rows": int(f[2]), "snapshots": int(s[0]),
                         "manifests": int(m[0]), "avg_file_kb": round(int(f[1]) / max(int(f[0]), 1) / 1024, 1)}
    save(f"files:{name}", {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "tables": out})
    return out


def dag(name, run_id):
    p = subprocess.run(["docker", "compose", "exec", "-T", "airflow", "airflow", "tasks", "states-for-dag-run",
                        "lakehouse_refresh", run_id, "-o", "json"], cwd=ROOT, capture_output=True, text=True)
    rows = json.loads(p.stdout[p.stdout.find("["):])
    # airflow dags test로 돌리면 시작 시각이 비어 있습니다. 태스크가 쉬지 않고 차례로 돌므로 앞 태스크의 끝을 시작으로 씁니다
    rows.sort(key=lambda r: r.get("end_date") or "")
    out, prev = {}, datetime.fromisoformat(rows[0]["execution_date"]) if rows else None
    for r in rows:
        s = datetime.fromisoformat(r["start_date"]) if r.get("start_date") else prev
        e = datetime.fromisoformat(r["end_date"]) if r.get("end_date") else None
        secs = (e - s).total_seconds() if s and e else None
        out[r["task_id"]] = {"state": r["state"], "seconds": round(secs, 1) if secs is not None else None}
        prev = e or prev
    first = datetime.fromisoformat(rows[0]["start_date"] or rows[0]["execution_date"]) if rows else None
    total = round((prev - first).total_seconds(), 1) if rows and prev else None
    save(f"dag:{name}", {"run_id": run_id, "tasks": out, "total_seconds": total})
    return out, total


def mem(name):
    p = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{.Name}}\t{{.MemUsage}}\t{{.CPUPerc}}"],
                       capture_output=True, text=True)
    out = {ln.split("\t")[0]: {"mem": ln.split("\t")[1], "cpu": ln.split("\t")[2]} for ln in p.stdout.splitlines() if ln}
    save(f"mem:{name}", {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "containers": out})
    return out


def show():
    res = load()
    for k, v in res.items():
        if k.startswith("bronze:"):
            r = v["run"]
            print(f"{k}: 매매 {r['trades']}건, 트랜잭션 {r['transactions']}, 쓰기 {r['write_seconds']}초")
            for t, x in v["tables"].items():
                if x["events"]:
                    print(f"  {t:<10} 이벤트 {x['events']:>7}  total p50/p95/p99 {x['total_ms']['p50_p95_p99']} max {x['total_ms']['max']}"
                          f"  capture p95 {x['capture_ms']['p50_p95_p99'][1]}  commit p95 {x['commit_ms']['p50_p95_p99'][1]}"
                          f"  비우기 {x['drain_s']}초, 초당 {x['events_per_s']}, 파일 {x['data_files']}, 스냅샷 {x['snapshots']}")
        elif k.startswith("dag:"):
            print(f"{k}: 전체 {v['total_seconds']}초 " + ", ".join(f"{t} {x['seconds']}" for t, x in v["tasks"].items()))
        elif k.startswith("files:"):
            print(f"{k}: " + ", ".join(f"{t.split('.', 1)[1]} 파일 {x['data_files']} 평균 {x['avg_file_kb']}KB 스냅샷 {x['snapshots']}"
                                       for t, x in v["tables"].items()))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "bronze":
        print(json.dumps(bronze(sys.argv[2])["tables"], ensure_ascii=False, indent=1))
    elif cmd == "wait":
        sys.exit(0 if wait(sys.argv[2]) else 1)
    elif cmd == "files":
        print(json.dumps(files(sys.argv[2]), ensure_ascii=False, indent=1))
    elif cmd == "dag":
        print(dag(sys.argv[2], sys.argv[3]))
    elif cmd == "mem":
        print(json.dumps(mem(sys.argv[2]), ensure_ascii=False, indent=1))
    elif cmd == "show":
        show()
    else:
        sys.exit(__doc__)
