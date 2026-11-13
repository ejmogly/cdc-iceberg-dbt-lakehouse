#!/usr/bin/env python3
"""SQL 파일 여러 개를 Spark Thrift server의 한 세션에서 차례로 돌립니다(pyhive).

infra/spark/sql.sh와 같은 일을 beeline 없이 합니다. Airflow 컨테이너처럼 spark 컨테이너 밖에서 씁니다.
앞 파일이 만든 임시 뷰를 뒤 파일이 쓰므로 파일을 한 번에 넘깁니다. 첫 오류에서 멈추고 1로 끝납니다.
  python run_sql.py sql/silver/orders/1_incoming.sql sql/silver/orders/2_merge.sql
"""
import os
import sys

import sqlparse
from pyhive import hive


def statements(path):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    for stmt in sqlparse.split(text):
        body = sqlparse.format(stmt, strip_comments=True).strip().rstrip(";").strip()
        if body:
            yield body


def main(paths):
    conn = hive.connect(
        host=os.environ.get("SPARK_THRIFT_HOST", "spark"),
        port=int(os.environ.get("SPARK_THRIFT_PORT", "10000")),
        username="run_sql", auth="NOSASL")
    cur = conn.cursor()
    for path in paths:
        n = 0
        for body in statements(path):
            cur.execute(body)
            n += 1
        print(f"{path}: 문장 {n}개", flush=True)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    try:
        main(sys.argv[1:])
    except Exception as e:  # 검사(assert_true) 실패나 접속 실패
        print(f"실패: {str(e)[:1000]}", file=sys.stderr)
        sys.exit(1)
