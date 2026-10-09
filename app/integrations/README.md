# ClauseGuard 集成适配层（IntegrationHub）开发指南

## 架构

```
业务代码(collab/main) --publish()--> 事件总线(hub.py) --异步--> 已启用Connector.send_event()
                                        |                        |
                                        |                        失败→ queue.py落
                                        |                        integration_outbox重试队列
                                        └--> subscribe()本地订阅器
```

- 事件类型统一定义在`events.py`（含e签宝预留事件`esign.status_changed`）
- Connector基类与注册表在`base.py`，注册表`CONNECTOR_CLASSES`：name→"模块:类名"
- 启用开关：环境变量`INTEGRATIONS_ENABLED=dingtalk,esignbao`（留空全部禁用）
- 配置集中在`app/config.py`（环境变量覆盖），Connector用`CONFIG_KEYS/REQUIRED_KEYS`声明式注入
- 所有外发与回调均写`audit_log`（action如`dingtalk_send`/`dingtalk_callback`/`outbox_retry`）

## 如何新增一个Connector（以e签宝为例）

1. **配置**：`app/config.py`新增`ESIGNBAO_APP_ID/ESIGNBAO_SECRET/ESIGNBAO_API_BASE`等（`os.environ.get`读取）
2. **事件**：需要新事件类型时在`events.py`加常量并加入`ALL_EVENTS`（`esign.status_changed`已预留）
3. **实现**：`app/integrations/esignbao/connector.py`补全`ESignBaoConnector`：
   - `send_event(event)`：按`event_type`分发；支持的事件返回True，不支持返回False跳过；网络失败直接抛异常（总线自动落重试队列，勿自行吞掉）
   - `send_message(user, title, content, **kw)`：发消息；`user`为ClauseGuard用户名，用`self.map_user()`映射外部标识
   - `health_check()`：返回`{ok, detail}`
   - 外发内容调用`dingtalk.connector.mask_sensitive()`做敏感字段脱敏（手机号/身份证/银行卡）
4. **回调路由**：在`routes.py`加`@router.post("/esignbao/callback")`，模式参照钉钉：body原文+query中timestamp/nonce+请求头签名→`hmac.compare_digest`验签→写审计→业务处理
5. **注册**：`base.py`的`CONNECTOR_CLASSES`加`"esignbao": "app.integrations.esignbao:ESignBaoConnector"`（已预登记）
6. **启用**：`INTEGRATIONS_ENABLED=esignbao`（重启生效），访问`GET /api/integrations/status`查看健康状态

## 自测

`python test_f7_integrations.py`：mock钉钉API（本地http.server）验证token获取、
工作通知发送、脱敏、事件派发、失败重试落库、禁用不发送、回调验签。
