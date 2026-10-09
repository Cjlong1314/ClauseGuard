# ClauseGuard私有化部署包

单机离线部署（FastAPI+PostgreSQL/pgvector+本地LLM），目标≤2小时完成。

## 一、离线准备（有网环境提前做，约30分钟）

1. 导出镜像（在可联网机器上）：
   ```
   docker pull pgvector/pgvector:pg16
   docker pull ollama/ollama:latest        # 或 vllm/vllm-openai:latest
   docker build -t clauseguard-app:2.0.0 -f deploy/Dockerfile .   # 或在有网机构建后导出
   docker save pgvector/pgvector:pg16 ollama/ollama:latest clauseguard-app:2.0.0 -o clauseguard_images.tar
   ```
2. 下载模型权重（提前打包，离线盘≥30GB）：
   - Ollama：有网机`ollama pull qwen2.5:14b`后打包`~/.ollama`目录
   - vLLM：下载`Qwen/Qwen2.5-14B-Instruct-AWQ`整个仓库（huggingface-cli download）+embedding模型bge-m3
3. 拷贝整个项目目录+镜像包到目标机。

## 二、单机部署步骤（目标≤2小时）

```
# 1. 导入镜像（约10分钟，含解压）
docker load -i clauseguard_images.tar

# 2. 配置环境变量
cd deploy
copy .env.example .env        # Linux: cp .env.example .env
#   按需修改：数据库口令、OLLAMA_RUNTIME（vllm|ollama）、模型名
#   注意：宿主机裸跑数据库时DATABASE_URL指127.0.0.1；compose内自动指向db服务

# 3. 启动数据库+应用（约15分钟，含初始化）
Windows: powershell -File scripts\deploy.ps1 install
Linux:   bash scripts/deploy.sh install
#   也可手工: docker compose up -d db && 等就绪 && docker compose up -d app

# 4. 加载模型（约20-40分钟，视磁盘速度）
# Ollama: docker cp <模型目录>/. clauseguard-ollama:/root/.ollama
#   或离线机: docker exec clauseguard-ollama ollama pull qwen2.5:14b（需临时网络）
# vLLM: 权重目录挂载到llm_models卷后启动 --profile llm
# 并把.env中LLM_BASE_URL_CONTAINER改为对应服务地址（vllm: http://llm:8000/v1；ollama: http://ollama:11434/v1）
# 重启应用生效: docker compose up -d app

# 5. 迁移规则→审查点库并向量化（约5分钟）
docker exec clauseguard-app python migrate_rules_to_checkpoints.py --embed

# 6. 健康检查验收
Windows: powershell -File scripts\health_check.ps1
Linux:   bash scripts/health_check.sh
# 三探针全ok即部署完成；LLM未配置为degraded属正常降级（规则引擎基线可用）
```

## 三、备份与恢复

- 每日全量：Linux crontab `30 2 * * * bash scripts/backup.sh`；Windows `schtasks`见backup.ps1头注释。
- WAL归档：PG `archive_mode=on`+archive_command归档到独立目录/盘（脚本头注释有配置样例），支持PITR任意时点恢复。
- 恢复：`bash scripts/restore.sh <dump文件>`或`backup.ps1 -Action restore -DumpFile <文件>`（会停应用、重建库、恢复后重启）。
- 恢复演练：每季度执行一次并留存记录（等保测评必查，见docs/等保三级建设路径.md）。

## 四、升级步骤

```
# 1. 升级前自动备份
Windows: powershell -File scripts\deploy.ps1 upgrade
Linux:   bash scripts/deploy.sh upgrade
# 2. 验证健康检查通过后观察1个业务周期
```
升级只重建app镜像，db数据卷不动；数据库结构变更由应用启动时幂等迁移完成。

## 五、回滚步骤

```
bash scripts/deploy.sh rollback      # 回到上一个app镜像tag
# 数据回滚: 用upgrade前的自动备份执行restore
```
前提：升级前的旧镜像未删除（`docker images clauseguard-app`可查历史tag）。

## 六、自测结果（2026-10-08）

- `app/health.py`新增/health三探针（db/llm/embedding降级可报），py_compile通过，本地uvicorn实测：database ok（pgvector array-fallback模式）、llm ok（实测在线网关）、embedding degraded（未配置），整体degraded——符合"降级可报"设计。
- `health_check.ps1`本机实测跑通：三探针输出正确（LLM网关无/models端点按HTTP4xx判在线），结论"降级可用"（exit 1）与/health整体状态一致；ps1文件已加UTF-8BOM（PowerShell 5.1中文必需）。
- `health_check.sh`为Linux/bash目标脚本（本机WSL不可用未实测，已在脚本内做pg_isready缺失降级处理）；部署机Linux环境执行即可。
- compose语法：本机无docker daemon，未能执行`docker compose config -q`校验；compose文件按Compose v2规范编写（profiles/healthcheck/depends_on.condition均为标准语法），建议到部署机执行`docker compose config -q`首验。
- python结构校验：deploy目录文件齐全（compose/Dockerfile/.env.example/initdb/scripts双栈）。

## 七、遗留项

1. 本机docker不可用：compose实际拉起与`docker compose config -q`校验需在部署机完成。
2. pgvector镜像已选（pgvector/pgvector:pg16），若客户已有PG实例需手动安装vector扩展（否则自动降级数组余弦，不阻塞）。
3. 模型压测未真跑（按任务要求仅文档），部署后按docs/模型选型与压测基线.md执行并产出报告。
4. HTTPS由前置Nginx/TongHTTP终结，compose未内置（等保P1项，附配置示例待补：可加gateway服务）。
5. 存储加密（AES-256/SM4）为等保整改P1项，需应用侧开发（uploads加密+pgcrypto），本包未含。
