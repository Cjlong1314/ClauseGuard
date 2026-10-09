# -*- coding: utf-8 -*-
"""F2数据迁移脚本：app/rules_data/rules.json → 审查点表（kb_checkpoints/kb_law_vectors）

用法：
  python migrate_rules_to_checkpoints.py            # 迁移，不向量化
  python migrate_rules_to_checkpoints.py --embed    # 迁移后补算向量（需配置EMBEDDING_*或EMBEDDING_FAKE=1）
幂等：按rule_id覆盖更新，可重复执行。
"""
import sys
sys.path.insert(0, ".")

from app import db, checkpoints

if __name__ == "__main__":
    db.init_db()
    mode = db.VECTOR_MODE
    print(f"[db] 建表完成，向量存储模式: {mode}"
          + ("" if mode != "array" else "（pgvector不可用，已降级为float8[]数组+余弦兜底）"))
    result = checkpoints.migrate_rules()
    print(f"[migrate] rules.json → 审查点 {result['checkpoints']} 条，"
          f"去重法条 {result['unique_laws']} 条")
    if "--embed" in sys.argv:
        sync = checkpoints.sync_embeddings()
        print(f"[embed] {sync}")
    print("[done] 迁移完成")
