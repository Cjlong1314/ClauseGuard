# ClauseGuard 合同智能审查系统（MVP）

依据《系统功能方案》V1.0 第一期范围实现：

- 合同上传与解析（DOCX/PDF/TXT，条款切分与编号还原）
- 合同要素抽取（主体、类型、金额、期限等，正则规则版）
- 智能合规审查（违法条款 / 缺失条款 / 风险条款 / 格式规范，规则引擎）
- 审查工作台（Web页面，风险列表定位条款）
- 审查报告导出（Word格式，docx生成）

## 目录结构

```
ClauseGuard/
├── docs/                # 需求文档
├── app/
│   ├── main.py          # FastAPI 入口
│   ├── config.py        # 配置
│   ├── models.py        # Pydantic 数据模型
│   ├── parser.py        # 文档解析与条款切分
│   ├── extractor.py     # 合同要素抽取
│   ├── rules.py         # 规则库加载
│   ├── rules_data/      # 内置规则（JSON）
│   ├── reviewer.py      # 审查引擎
│   ├── report.py        # 审查报告生成（docx）
│   └── templates/       # 前端页面
├── storage/             # 上传文件与报告存放（运行时生成）
├── requirements.txt
└── README.md
```

## 数据库（PostgreSQL）

数据存储改为PostgreSQL（不再使用JSON文件/内存）：

- 合同、用户、会话、法规库、模板库、协同任务、通知、审计留痕均在`clauseguard`库
- 连接串通过环境变量`DATABASE_URL`覆盖，默认`postgresql://clauseguard:Cg%402026pg@127.0.0.1:5432/clauseguard`
- 首次启动自动建表（幂等），无需手工执行SQL
- 数据库初始化脚本：`python setup_db.py`（创建数据库与专用账号，需要超级用户权限）

## 启动

```
pip install -r requirements.txt
python -m uvicorn app.main:app --reload --port 8600
```

浏览器访问 http://127.0.0.1:8600 即可打开审查工作台。

## F1 批注回写与版本比对

- 批注回写：审查发现按条款锚点（clause_id+文本相似度定位段落）以OOXML批注（word/comments.xml）写入合同原文，每条批注含风险等级、规则名、法条依据、修改建议；无法定位锚点的发现降级附加到文档末尾清单。支持带批注版与干净版（剥离批注）两份docx导出。
- 条款级版本比对：两版合同按clause_id+文本相似度做条款树对齐，段落级diff用difflib，标出新增/删除/修改条款；金额/期限/责任类条款变更单独标记为高危。
- 自测脚本：`python test_f1.py`（生成测试合同docx跑通 parser→reviewer→annotator→diff 全流程）。

## 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | /api/contracts/upload | 上传合同并解析 |
| GET | /api/contracts | 合同列表 |
| GET | /api/contracts/{cid} | 合同详情（含条款树） |
| POST | /api/contracts/{cid}/review | 执行审查 |
| GET | /api/contracts/{cid}/report | 导出审查报告docx |
| GET | /api/contracts/{cid}/report/annotated?mode=annotated\|clean | 导出批注式审查报告（带批注版/干净版docx，F1） |
| POST | /api/contracts/{cid}/diff | 条款级版本比对，body:{target_id}（F1） |
| GET | /diff | 条款级版本比对页面（F1） |
| GET | /api/rules | 查看规则库 |
| GET | /api/contracts/{cid}/parse-status | F5：扫描件OCR解析状态查询（processing/done/failed） |

## F5 OCR/PDF版式解析（可选依赖）

扫描件/图片PDF自动走PaddleOCR识别通道（CPU可用），结果带置信度，低置信段落（平均置信度<`OCR_MIN_CONF`，默认0.85）标记需人工复核；签署栏区域按关键词识别。未安装OCR依赖时扫描件上传返回中文安装指引（不影响DOCX/TXT/文本层PDF）。

安装（CPU版，Windows/Linux通用）：
```
pip install paddlepaddle==3.3.1
pip install paddleocr==3.7.0
```
首次运行自动下载模型至`~/.paddlex/official_models`。相关环境变量：`OCR_ENABLED`（默认开）、`OCR_LANG`（默认ch）、`OCR_MIN_CONF`、`OCR_SCAN_MIN_CHARS`（文本层字符数扫描件判定阈值，默认50）、`OCR_DPI`（默认150）、`OCR_ASYNC`（默认开，扫描件上传先落"解析中"状态+daemon线程后台解析，零新增依赖）。

自测脚本：`python test_f5.py`（模块级）、`python test_f5_e2e.py`（需服务运行中）。
