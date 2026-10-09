"""集成适配层包（F7）：
- events.py  事件类型定义（含e签宝预留事件）
- base.py    Connector基类+注册表
- hub.py     事件总线（publish/subscribe，异步派发）
- queue.py   失败重试队列（outbox落库）
- dingtalk/  钉钉Connector
- esignbao/  e签宝Connector（预留桩，待实现）
- routes.py  回调webhook路由
"""
