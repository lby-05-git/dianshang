# -*- coding: utf-8 -*-
"""Worker 启动入口：python -m app.worker.run

启动三个消费线程（申请流 / 决策流 / 评测流）；Ctrl+C 优雅退出。
结构化日志：JSON 格式 + case_id/trace_id 上下文字段（docs/08 第5阶段要求）。

启动路径：eval_refund 包在项目根目录（与 backend 平级），而 `python -m app.worker.run`
默认只把 backend 加进 sys.path；这里把项目根也注入，保证不管从哪启动都能 import。
"""
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

# eval_refund 在项目根（backend 上一级）：与 tests/conftest.py 同样的根路径注入
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.worker.decision_processor import HumanDecisionConsumer
from app.worker.eval_processor import EvalRunConsumer
from app.worker.refund_processor import RefundApplyConsumer


def _scheduler_poll(stop: threading.Event, interval: float = 12.0) -> None:
    """V2.0 定时评测触发哨兵：周期触发到期调度 + 巡检失败告警。

    - fire_due_schedules：daily/weekly/change 到期即触发 Run（manual 绝不自动）；
    - raise_run_failure_alerts：定时触发的 Run FAILED → 落 RUN_FAILED 告警。
    只观测与触发评测，绝不自动改任何决策/退款（评测隔离红线）。
    """
    from app.infrastructure.db import SessionLocal
    from app.services import eval_center as ec
    logger = logging.getLogger("worker.scheduler")
    while not stop.wait(interval):
        db = SessionLocal()
        try:
            # 意图路由：默认每日意图对照评测。幂等创建，已停用的调度不会被重建。
            from app.services import intent_periodic_eval as ipe
            ipe.ensure_default_schedule(db)
            fired = ec.fire_due_schedules(db, created_by="scheduler")
            if fired["count"]:
                logger.info("scheduled eval fired: %s",
                            [f["run_id"] for f in fired["fired"]])
            alerts = ec.raise_run_failure_alerts(db)
            if alerts["count"]:
                logger.warning("scheduled eval failure alerts: %d", alerts["count"])
        except Exception:  # noqa: BLE001 —— 哨兵异常不拖垮整个 worker
            logger.exception("scheduler poll failed")
        finally:
            db.close()


def _dead_letter_poll(stop: threading.Event, interval: float = 30.0) -> None:
    """V2.0 -4 死信消费者：周期重驱 OPEN 退款类死信（幂等，不重复退款）。

    - refund 死信走 `dead_letter.replay_dead_letter` 内置 retry_refund（同一 replay_guid
      落唯一 REPLAY 事件，绝不产生第二笔退款）；
    - 非 refund / 缺 case_id：置 FAILED 等待人工修复，绝不假装重放成功；
    - 并发领取由条件 UPDATE 保证，双 Worker 不会同时重驱同一死信（V-R5 红线）。
    """
    from app.infrastructure.db import SessionLocal
    from app.services import dead_letter as dl
    logger = logging.getLogger("worker.dead_letter")
    while not stop.wait(interval):
        db = SessionLocal()
        try:
            opened = dl.list_dead_letters(
                db, status=dl.STATUS_OPEN, task_type=dl.TASK_REFUND, limit=50)
            for task in opened:
                out = dl.replay_dead_letter(db, task["id"], replayed_by="worker")
                if out["replayed"]:
                    logger.info("dead-letter replayed: %s", task["id"])
                elif out.get("skipped"):
                    logger.info("dead-letter idempotent-skip: %s (%s)",
                                task["id"], out["skipped"])
                else:
                    logger.warning("dead-letter replay failed: %s %s",
                                   task["id"], out.get("error"))
        except Exception:  # noqa: BLE001 —— 死信巡检异常不拖垮整个 worker
            logger.exception("dead-letter poll failed")
        finally:
            db.close()


def _install_capability_plugin() -> None:
    """Worker 启动时安装基线四能力插件（unavailable -> available + metric 注册）。"""
    try:
        from app.services.capability_plugin import install
        install()
    except Exception:  # noqa: BLE001 —— 插件失败不阻断 worker 启动
        logging.getLogger("worker.run").exception("capability_plugin install failed")


def _default_hosted_services() -> list[str]:
    """本 Worker 实例托管的能力域（V3-6 独立扩缩容就绪标记）。

    默认 = evaluation（评测消费+调度哨兵）；实际部署可经环境变量
    WORKER_SERVICES（逗号分隔，如 "evaluation,intent"）声明本实例额外托管的域，
    支持各域独立水平扩缩容（多个 Worker 托管同一域 -> 实例数叠加）。
    """
    raw = os.environ.get("WORKER_SERVICES", "evaluation")
    return [s.strip() for s in raw.split(",") if s.strip()]


def _mark_readiness(instance: str, services: list[str], ready: bool) -> None:
    """就绪标记写入（best-effort，绝不因标记失败拖垮 worker 启停）。"""
    from app.services import readiness as rd

    for svc in services:
        try:
            if ready:
                rd.mark_ready(svc, instance=instance)
            else:
                rd.mark_not_ready(svc, instance=instance)
        except Exception:  # noqa: BLE001 —— 标记失败只记录
            logging.getLogger("worker.run").exception(
                "readiness mark %s %s failed", "ready" if ready else "not_ready", svc)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        return json.dumps(entry, ensure_ascii=False)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger().handlers[0].setFormatter(JsonFormatter())
    _install_capability_plugin()

    stop = threading.Event()
    consumers = [RefundApplyConsumer(), HumanDecisionConsumer(), EvalRunConsumer()]
    threads = [threading.Thread(target=c.run, args=(stop,), daemon=True) for c in consumers]
    # V2.0：定时评测哨兵（到期触发 + 失败告警巡检）
    threads.append(threading.Thread(target=_scheduler_poll, args=(stop,), daemon=True))
    # V2.0 -4：死信消费者（退款类死信幂等重驱，不重复退款）
    threads.append(threading.Thread(target=_dead_letter_poll, args=(stop,), daemon=True))
    for t in threads:
        t.start()
    # V3-6 ：本实例声明其托管能力域就绪（独立扩缩容标记；退出时撤标）
    served = _default_hosted_services()
    worker_instance = f"worker-{os.getpid()}"
    _mark_readiness(worker_instance, served, ready=True)
    logging.getLogger("worker.run").info("worker started: %s + scheduler (ready: %s)",
                                         [c.group for c in consumers], served)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        stop.set()
        _mark_readiness(worker_instance, served, ready=False)
        logging.getLogger("worker.run").info("worker stopping")


if __name__ == "__main__":
    main()
