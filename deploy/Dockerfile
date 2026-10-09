# ClauseGuard应用镜像（FastAPI，依赖在requirements.txt）
FROM python:3.11-slim

WORKDIR /app

# 先装依赖利用层缓存
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ /app/app/
COPY setup_db.py migrate_json_to_pg.py migrate_rules_to_checkpoints.py /app/

EXPOSE 8600
# 不在此处init_db：应用启动时幂等自动建表（db.py）
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8600"]
