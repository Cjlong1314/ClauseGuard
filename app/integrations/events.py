"""系统事件定义（F7集成适配层）

事件类型常量统一在此定义，新增Connector（如e签宝）所需的事件也在此声明，
避免字符串散落各处。事件载荷统一为dict，含event_type/envelope公共字段由EventBus填充。
"""

# ---- 通用系统事件 ----
EVENT_REVIEW_COMPLETED = "review.completed"        # 合同审查完成
EVENT_TASK_ASSIGNED = "task.assigned"              # 审查任务分配
EVENT_TASK_STATUS_CHANGED = "task.status_changed"  # 任务状态流转
EVENT_TASK_REMIND = "task.remind"                  # 任务催办
EVENT_EXPIRY_REMIND = "contract.expiry_remind"     # 合同到期提醒（F8复用）

# ---- 电子签事件（为e签宝Connector预留） ----
EVENT_ESIGN_STATUS_CHANGED = "esign.status_changed"  # 签署状态回写（发起/签署中/完成/拒签/撤销）
# e签宝签署状态枚举约定：INITIATED / SIGNING / COMPLETED / REJECTED / CANCELLED
ESIGN_STATUSES = ("INITIATED", "SIGNING", "COMPLETED", "REJECTED", "CANCELLED")

# 所有合法事件类型（派发时校验）
ALL_EVENTS = {
    EVENT_REVIEW_COMPLETED, EVENT_TASK_ASSIGNED, EVENT_TASK_STATUS_CHANGED,
    EVENT_TASK_REMIND, EVENT_EXPIRY_REMIND, EVENT_ESIGN_STATUS_CHANGED,
}
