# -*- coding: utf-8 -*-
"""测试基座：独立测试库 complaint_refund_test（不污染开发库）+ 依赖覆盖。"""
import sys
from pathlib import Path

# 让 backend 测试可 import 根目录共享包 eval_refund（worker 评测内核）
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import pymysql
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.domain import models
from app.infrastructure.db import get_db
from app.infrastructure.seed import seed
from app.main import app

TEST_DB_NAME = "complaint_refund_test"
TEST_DB_URL = f"mysql+pymysql://root:123456@127.0.0.1:3306/{TEST_DB_NAME}?charset=utf8mb4"

engine = create_engine(TEST_DB_URL, pool_pre_ping=True)
SessionTest = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture(scope="session", autouse=True)
def _prepare_test_db():
    conn = pymysql.connect(host="127.0.0.1", user="root", password="123456", charset="utf8mb4")
    cur = conn.cursor()
    cur.execute(
        f"CREATE DATABASE IF NOT EXISTS {TEST_DB_NAME} "
        "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
    )
    conn.close()
    models.Base.metadata.create_all(engine)
    # 任务四审计约束：create_all 不建约束/索引，手动补齐（幂等）
    from sqlalchemy import text

    with engine.begin() as conn:
        try:
            conn.execute(text(
                "ALTER TABLE human_interventions "
                "ADD CONSTRAINT uq_human_interventions_case_id UNIQUE (case_id)"
            ))
        except Exception:  # noqa: BLE001 —— 已存在则跳过
            pass
        # 评测中心 V2.0（迁移 f1a2b3c4d5e6）：eval_runs.run_id 唯一约束，
        # create_all 不建约束，须在 conftest 幂等补齐（与迁移一致）。
        try:
            conn.execute(text(
                "ALTER TABLE eval_runs "
                "ADD CONSTRAINT uq_eval_runs_run_id UNIQUE (run_id)"
            ))
        except Exception:  # noqa: BLE001 —— 已存在/表不存在则跳过
            pass
        # 评测中心 V3.0（迁移 a1b2c3d4e5f6）：eval_schedules 唯一约束
        # create_all 会建表但不会建约束，须幂等补齐（与迁移一致）。
        try:
            conn.execute(text(
                "ALTER TABLE eval_schedules "
                "ADD CONSTRAINT uq_eval_schedule_suite_tier UNIQUE (suite_id, tier)"
            ))
        except Exception:  # noqa: BLE001 —— 已存在/表不存在则跳过
            pass
        # V3.0：eval_suites.tier_map 列（若是旧库 create_all 不重建表，需补列）
        try:
            conn.execute(text(
                "ALTER TABLE eval_suites ADD COLUMN tier_map JSON"
            ))
        except Exception:  # noqa: BLE001 —— 已存在则跳过
            pass
        # M19 发布门禁（迁移 b2c3d4e5f6a7）：create_all 会建表但无约束/索引，
        # 幂等补约束/索引（与迁移一致；旧库未建表则跳过）。
        try:
            conn.execute(text(
                "ALTER TABLE eval_baselines "
                "ADD CONSTRAINT uq_eval_baseline_suite_role UNIQUE (suite_id, role)"
            ))
        except Exception:  # noqa: BLE001 —— 已存在则跳过
            pass
        for _idx, _sql in (
            ("idx_eval_baseline_suite_role",
             "CREATE INDEX idx_eval_baseline_suite_role "
             "ON eval_baselines (suite_id, role)"),
            ("idx_eval_gate_run",
             "CREATE INDEX idx_eval_gate_run ON eval_gate_results (run_id)"),
            ("idx_eval_budget_dim",
             "CREATE INDEX idx_eval_budget_dim ON eval_budgets (dimension, dim_key)"),
            ("idx_eval_drift_run",
             "CREATE INDEX idx_eval_drift_run ON eval_drift_indicators (run_id)"),
            ("idx_eval_alert_run",
             "CREATE INDEX idx_eval_alert_run ON eval_alerts (run_id)"),
        ):
            try:
                conn.execute(text(_sql))
            except Exception:  # noqa: BLE001 —— 已存在则跳过
                pass
        # M20 预算约束：unique(dimension, dim_key, period)
        try:
            conn.execute(text(
                "ALTER TABLE eval_budgets "
                "ADD CONSTRAINT uq_eval_budget_dim_period "
                "UNIQUE (dimension, dim_key, period)"
            ))
        except Exception:  # noqa: BLE001 —— 已存在则跳过
            pass
        # V3.0 V3-1（迁移 a4b5c6d7e8f9）：reconciliation_events.guid 唯一约束
        # （对账差异幂等：同 run 同案同差异不重复落审计）
        try:
            conn.execute(text(
                "ALTER TABLE reconciliation_events "
                "ADD CONSTRAINT uq_recon_event_guid UNIQUE (guid)"
            ))
        except Exception:  # noqa: BLE001 —— 已存在/表不存在则跳过
            pass
        # V3.0 V3-3（迁移 b5c6d7e8f9a0）：tool_definitions/version_registry 唯一约束
        # create_all 会建表但可能不建约束，幂等补齐（与迁移一致）。
        try:
            conn.execute(text(
                "ALTER TABLE tool_definitions "
                "ADD CONSTRAINT uq_tool_definitions_name UNIQUE (tool_name)"
            ))
        except Exception:  # noqa: BLE001 —— 已存在/表不存在则跳过
            pass
        try:
            conn.execute(text(
                "ALTER TABLE version_registry "
                "ADD CONSTRAINT uq_ver_reg_entity_version "
                "UNIQUE (entity_type, entity_name, version)"
            ))
        except Exception:  # noqa: BLE001 —— 已存在/表不存在则跳过
            pass
        try:
            conn.execute(text(
                "CREATE INDEX idx_ver_reg_entity "
                "ON version_registry (entity_type, entity_name)"
            ))
        except Exception:  # noqa: BLE001 —— 已存在则跳过
            pass
        # V3.0 V3-4（迁移 c6d7e8f9a0b1）：intent_classifier_versions / intent_replay_queue 约束
        try:
            conn.execute(text(
                "ALTER TABLE intent_classifier_versions "
                "ADD CONSTRAINT uq_intent_cls_name_version "
                "UNIQUE (classifier_name, version)"
            ))
        except Exception:  # noqa: BLE001 —— 已存在/表不存在则跳过
            pass
        try:
            conn.execute(text(
                "CREATE INDEX idx_intent_cls_name "
                "ON intent_classifier_versions (classifier_name)"
            ))
        except Exception:  # noqa: BLE001 —— 已存在则跳过
            pass
        try:
            conn.execute(text(
                "ALTER TABLE intent_replay_queue "
                "ADD CONSTRAINT uq_intent_replay_sample UNIQUE (sample_id)"
            ))
        except Exception:  # noqa: BLE001 —— 已存在/表不存在则跳过
            pass
        try:
            conn.execute(text(
                "CREATE INDEX idx_intent_replay_status "
                "ON intent_replay_queue (status)"
            ))
        except Exception:  # noqa: BLE001 —— 已存在则跳过
            pass
        # V3.0 V3-6（迁移 d7e8f9a0b1c2）：trace_local_queue / release_evidence_packages 约束
        try:
            conn.execute(text(
                "ALTER TABLE trace_local_queue "
                "ADD CONSTRAINT uq_trace_lq_delivery UNIQUE (delivery_key)"
            ))
        except Exception:  # noqa: BLE001 —— 已存在/表不存在则跳过
            pass
        try:
            conn.execute(text(
                "CREATE INDEX idx_trace_lq_status "
                "ON trace_local_queue (status, enqueued_at)"
            ))
        except Exception:  # noqa: BLE001 —— 已存在则跳过
            pass
        try:
            conn.execute(text(
                "ALTER TABLE release_evidence_packages "
                "ADD CONSTRAINT uq_evp_run_suite_version "
                "UNIQUE (run_id, suite_id, package_version)"
            ))
        except Exception:  # noqa: BLE001 —— 已存在则跳过
            pass
        try:
            conn.execute(text(
                "ALTER TABLE release_evidence_packages "
                "ADD CONSTRAINT uq_evp_fingerprint UNIQUE (fingerprint)"
            ))
        except Exception:  # noqa: BLE001 —— 已存在则跳过
            pass
        try:
            conn.execute(text(
                "CREATE INDEX idx_evp_run ON release_evidence_packages (run_id)"
            ))
        except Exception:  # noqa: BLE001 —— 已存在则跳过
            pass
        # V1.0 评测列（迁移 e1f2a3b4c5d6）--- 测试库 create_all 不重建既有表，
        # 必须在 conftest 幂等补列，与迁移一致。同现约束：已存在则跳过。
        _EVAL_COLUMNS = {
            "order_verification": "VARCHAR(20)",
            "order_verification_reasons": "JSON",
            "product_verification": "VARCHAR(20)",
            "product_verification_reasons": "JSON",
            "price_verification": "VARCHAR(20)",
            "price_verification_reasons": "JSON",
            "eval_run_id": "VARCHAR(64)",
            "eval_case_id": "VARCHAR(64)",
        }
        for col, ddl in _EVAL_COLUMNS.items():
            try:
                conn.execute(text(
                    f"ALTER TABLE refund_cases ADD COLUMN {col} {ddl}"
                ))
            except Exception:  # noqa: BLE001 —— 已存在则跳过
                pass
        try:
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS idx_refund_cases_eval_run "
                "ON refund_cases (eval_run_id)"
            ))
        except Exception:  # noqa: BLE001 —— 已存在/MySQL 无 IF NOT EXISTS 则跳过
            pass
        # Phase O（迁移 d1e2f3a4b5c6）：agent_execution_logs 新增 trace/model 列，
        # create_all 不重建既有表，幂等补列（与迁移一致；已存在则跳过）。
        for _col, _ddl in (("trace", "JSON"), ("model", "VARCHAR(80)")):
            try:
                conn.execute(text(
                    f"ALTER TABLE agent_execution_logs ADD COLUMN {_col} {_ddl}"
                ))
            except Exception:  # noqa: BLE001 —— 已存在则跳过
                pass
        # 基线 V1.0（迁移 e2f3a4b5c6d7）：refund_cases 新增 intent/gateway_result 列
        # create_all 不重建既有表，幂等补列（与迁移一致；已存在则跳过）。
        for _col, _ddl in (("intent", "VARCHAR(32)"), ("gateway_result", "VARCHAR(16) NOT NULL DEFAULT 'PASS'")):
            try:
                conn.execute(text(
                    f"ALTER TABLE refund_cases ADD COLUMN {_col} {_ddl}"
                ))
            except Exception:  # noqa: BLE001 —— 已存在则跳过
                pass
        # V2.0 Phase 3（迁移 f3e4d5c6b7a9）：security_decisions.case_id 放宽为 NULL
        # （llm-critic 独立检查无案件归属时以审计行留存；FK 保留，可空）
        try:
            conn.execute(text(
                "ALTER TABLE security_decisions MODIFY case_id VARCHAR(36) NULL"
            ))
        except Exception:  # noqa: BLE001 —— 已存在则跳过
            pass
        # 基线 V2.0（迁移 f2e3d4c5b6a8）：7 张新表由 create_all 创建；既有表扩展列
        # create_all 不重建既有表，幂等补列（与迁移一致；已存在则跳过）。
        for _table, _cols in (
            ("refund_cases", (("refund_events_guid", "VARCHAR(64)"),
                              ("last_order_snapshot_id", "VARCHAR(36)"))),
            ("intent_results", (("conflict_candidates", "JSON"),
                                ("route_source", "VARCHAR(16)"))),
            ("voice_sessions", (("reconnects", "INTEGER NOT NULL DEFAULT 0"),
                                ("weak_signal", "TINYINT(1) NOT NULL DEFAULT 0"),
                                ("closed_reason", "VARCHAR(30)"))),
            ("eval_cases", (("category", "VARCHAR(20)"),)),
            ("eval_runs", (("trigger_source", "VARCHAR(20)"),)),
            ("coaching_sessions", (("difficulty", "VARCHAR(20)"),
                                   ("emotion", "VARCHAR(20)"),
                                   ("scene_name", "VARCHAR(64)"))),
        ):
            for _col, _ddl in _cols:
                try:
                    conn.execute(text(
                        f"ALTER TABLE {_table} ADD COLUMN {_col} {_ddl}"
                    ))
                except Exception:  # noqa: BLE001 —— 已存在则跳过
                    pass
    with SessionTest() as s:
        seed(db=s)  # 演示账号 + 阈值配置（幂等）


@pytest.fixture(autouse=True)
def _clear_login_rate_limit():
    """登录限流键在 Redis（多 worker 共享），测试间清空避免共享 IP 触发 429。"""
    from app.infrastructure.redis_client import redis_client

    keys = redis_client.keys("login:fail:*")
    if keys:
        redis_client.delete(*keys)
    yield


@pytest.fixture()
def client():
    def override_get_db():
        db = SessionTest()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
