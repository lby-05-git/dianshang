# -*- coding: utf-8 -*-
"""种子数据（幂等，可重复执行）：演示账号 + system_configs 阈值（docs/06 ）。

V2 扩展（3.2.2 权限模型 + 3.3.1 班组）：
  - 新增 leader1（班组长演示，数据范围=team，所属班组 leader_team）；
  - 演示账号全部补 data_scope（客服=self / 主管=team / 领导=store / 管理员=all）。
"""
from app.core.security import hash_password
from app.domain.models import (
    DATA_SCOPE_ALL,
    DATA_SCOPE_SELF,
    DATA_SCOPE_STORE,
    DATA_SCOPE_TEAM,
    ROLE_ADMIN,
    ROLE_CUSTOMER_SERVICE,
    ROLE_LEADER,
    ROLE_SUPERVISOR,
    SystemConfig,
    Team,
    TeamMember,
    User,
)
from app.infrastructure.db import SessionLocal

DEFAULT_CONFIGS = [
    ("refund.amount_threshold", {"value": 300}, "金额超阈值转人工（元）"),
    ("fraud.suspend_threshold", {"value": 60}, "欺诈分 >= 该值强制转人工"),
    ("fraud.auto_pass_threshold", {"value": 20}, "欺诈分 <= 该值可自动秒退"),
    ("ocr.confidence_threshold", {"value": 0.6}, "OCR 置信度低于该值转人工"),
]

# 演示账号：(username, password, role, data_scope, 描述)
DEMO_USERS = [
    ("customer_service1", "123456", ROLE_CUSTOMER_SERVICE, DATA_SCOPE_SELF, "客服演示账号"),
    ("supervisor1", "123456", ROLE_SUPERVISOR, DATA_SCOPE_TEAM, "主管演示账号（可审批）"),
    ("leader1", "123456", ROLE_LEADER, DATA_SCOPE_TEAM, "班组长演示账号（可审批/导出/协调）"),
    ("admin1", "123456", ROLE_ADMIN, DATA_SCOPE_ALL, "管理员演示账号（全量）"),
]

# 演示班组：客服+主管归属 leader_team（供协同台/数据范围验证）
_DEMO_TEAM_ID = "team-demo"
_DEMO_TEAM_NAME = "演示班组-A"


def seed(db=None) -> None:
    """幂等种子；db 为空时使用开发库会话（测试可传入测试库会话）。"""
    db = db or SessionLocal()
    try:
        for username, password, role, scope, _desc in DEMO_USERS:
            if not db.query(User).filter_by(username=username).first():
                db.add(User(username=username, password_hash=hash_password(password),
                            role=role, data_scope=scope))
        for key, value, desc in DEFAULT_CONFIGS:
            if not db.query(SystemConfig).filter_by(cfg_key=key).first():
                db.add(SystemConfig(cfg_key=key, cfg_value=value, description=desc))

        # V2 演示班组（幂等）：supervisor1 / leader1 归属 team-demo；customer_service1 独立
        if not db.query(Team).filter_by(id=_DEMO_TEAM_ID).first():
            db.add(Team(id=_DEMO_TEAM_ID, name=_DEMO_TEAM_NAME))
        for username in ("supervisor1", "leader1"):
            u = db.query(User).filter_by(username=username).first()
            if u and not db.query(TeamMember).filter_by(user_id=u.id, team_id=_DEMO_TEAM_ID).first():
                db.add(TeamMember(team_id=_DEMO_TEAM_ID, user_id=u.id))
        db.commit()
        print("seed ok")
    finally:
        db.close()


if __name__ == "__main__":
    seed()