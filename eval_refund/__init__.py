# -*- coding: utf-8 -*-
"""退赔评测包（与评测体系融合，F15 章节）。

V1.0 交付：
- CandidateOutput schema 1.0 + extensions 命名空间（-T06）
  `extensions.security / intent / voice / coaching` 仅为协议占位，不实现扩展能力业务。
- RefundSystemHttpTarget（黑盒 HTTP）：登录/建单/轮询/识别挂起/
  审批/拒绝/读终态/清理；目标不可用必须明确失败，不得伪造 pass。
"""