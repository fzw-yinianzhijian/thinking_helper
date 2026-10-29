"""Run fixed offline regression checks; never execute a model proposal."""
import argparse
import json
import os

from app.core import Store
from app.evaluation import CHECKS, ROOT, plan_evaluation, run_evaluation


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="对人工方法变更运行固定本地unittest并保存证据；不执行模型命令、不改运行策略。",
        epilog="baseline_only=仅基线；checks_failed=失败/超时；checks_passed=不同版本的变更通过所列回归，仍不代表产品价值。rollback_ref只记录，不自动回退。",
    )
    parser.add_argument("--checks", nargs="+", choices=list(CHECKS), default=["core", "planning"], help="固定检查集合；all为全套发现，不能与其他选项合用")
    parser.add_argument("--topic-id", help="关联长期问题ID，可选")
    parser.add_argument("--candidate-id", help="关联方法提案ID；接受提案不等于实施或验证")
    parser.add_argument("--baseline-ref", help="先前eval-开头的评估ID；其他字符串仅作备注，不证明基准版本")
    parser.add_argument("--rollback-ref", help="人工指定的已知恢复引用；仅记录，不执行checkout或恢复")
    parser.add_argument("--change-ref", help="人工实施的变更说明或提交引用；不会作为命令执行")
    parser.add_argument("--timeout", type=float, default=120, help="整次检查的超时秒数，最多1800")
    parser.add_argument("--database", default=os.environ.get("TH_DATABASE", str(ROOT / "data/workspace.sqlite3")), help="工作区SQLite路径")
    parser.add_argument("--output-dir", default=str(ROOT / "data/evaluations"), help="评估JSON目录")
    parser.add_argument("--dry-run", action="store_true", help="只列实际会执行的固定命令，不运行、不写JSON、不追加事件")
    args = parser.parse_args(argv)
    try:
        if args.dry_run:
            print(json.dumps(plan_evaluation(checks=args.checks, timeout_seconds=args.timeout), ensure_ascii=False, indent=2))
            return 0
        store = Store(args.database)
        try:
            record = run_evaluation(store, topic_id=args.topic_id, candidate_id=args.candidate_id,
                                    checks=args.checks, baseline_ref=args.baseline_ref,
                                    rollback_ref=args.rollback_ref, change_ref=args.change_ref,
                                    timeout_seconds=args.timeout, output_dir=args.output_dir)
        finally:
            store.close()
        summary = {key: record[key] for key in ("id", "status", "reason", "runtime_revision", "baseline_revision", "record_path", "event_id", "strategy_changed", "product_value_verified")}
        summary["checks"] = [{key: item[key] for key in ("check", "returncode", "tests_run", "timed_out", "passed")} for item in record["results"]]
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 1 if record["status"] == "checks_failed" else 0
    except (ValueError, KeyError, OSError) as error:
        parser.exit(2, f"评估未完成：{error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
