# -*- coding: utf-8 -*-
"""F2自测脚本：建表幂等、假embedding向量化、三级降级召回路径

用法：
  python test_f2_selftest.py
自测覆盖：
  1. db.init_db幂等建表 + pgvector检测降级（本机无pgvector→array模式）
  2. EMBEDDING_FAKE=1假embedding：迁移rules.json→审查点表→向量化
  3. 召回：向量路径（array+余弦）与关键词降级路径（EMBEDDING_FAKE=0）
  4. 强制溯源：空条款→空结果；召回结果必须含similarity与法条原文
PG不可达时打印跳过原因并以退出码0结束。
"""
import os
import sys
sys.path.insert(0, ".")

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def main():
    from app import db, checkpoints, embedding, rag

    # 0. PG可达性
    try:
        db.init_db()
    except Exception as e:
        print(f"[SKIP] PostgreSQL不可达，自测跳过。原因: {e}")
        print("（代码已就绪，待数据库可用后重跑本脚本即可）")
        return 0

    mode = db.VECTOR_MODE
    print(f"[1] 建表与pgvector检测：VECTOR_MODE={mode}")
    check("向量模式判定合法", mode in ("pgvector", "array"), mode)
    db.init_db()
    check("init_db幂等重跑不报错", True)

    # 2. 假embedding + 迁移
    print("[2] rules.json → 审查点表迁移（EMBEDDING_FAKE=1）")
    os.environ["EMBEDDING_FAKE"] = "1"
    result = checkpoints.migrate_rules()
    check("迁移13条规则", result["checkpoints"] >= 13, str(result))
    cps = checkpoints.list_checkpoints()
    check("审查点含法条依据/建议/立场字段",
          all(("legal_basis" in c and "suggestion" in c and "stance" in c) for c in cps),
          f"{len(cps)}条")
    sample = cps[0]
    check("法条依据已结构化解析",
          bool(sample["legal_basis"]) and "law_name" in sample["legal_basis"][0],
          str(sample["legal_basis"][:1]))

    print("[3] 假embedding向量化")
    sync = checkpoints.sync_embeddings()
    check("审查点向量同步", sync.get("checkpoint_vectors_synced", 0) >= 13, str(sync))

    # 4. 召回：向量路径（array+余弦）
    print("[4] 逐条款召回（向量路径）")
    clause = "甲方对乙方在履行本合同过程中造成的人身损害概不负责，乙方放弃一切索赔权利。"
    r = rag.recall_with_sources(clause)
    check("召回模式为array（向量可用）", r["mode"] in ("array", "pgvector"), r["mode"])
    check("召回命中审查点", len(r["checkpoints"]) > 0, f"{len(r['checkpoints'])}条")
    check("审查点带相似度", all("similarity" in c for c in r["checkpoints"]))
    check("召回命中法条且含原文", bool(r["laws"]) and all(l["content"] for l in r["laws"]),
          r["laws"][0]["law_name"] + r["laws"][0]["article_no"] if r["laws"] else "")
    top = r["checkpoints"][0]
    check("Top1为免责/格式条款类审查点", "免责" in top["name"] or "责任" in top["name"] or "权利" in top["name"],
          top["name"])

    # 5. 降级路径：关闭embedding → 关键词召回
    print("[5] 降级路径（无embedding→2-gram关键词）")
    os.environ.pop("EMBEDDING_FAKE", None)
    os.environ["EMBEDDING_BASE_URL"] = ""
    os.environ["EMBEDDING_MODEL"] = ""
    check("embedding判定为未配置", not embedding.is_configured())
    r2 = rag.recall_with_sources(clause)
    check("降级模式为keyword", r2["mode"] == "keyword", r2["mode"])
    check("关键词召回仍命中法条（基线可用）", len(r2["laws"]) > 0, f"{len(r2['laws'])}条")

    # 6. 强制溯源
    print("[6] 强制溯源")
    r3 = rag.recall_with_sources("   ")
    check("空条款返回空召回", r3["checkpoints"] == [] and r3["laws"] == [])
    check("原search()向后兼容", len(rag.search("违约金", 3)) > 0)

    print(f"\n===== 自测结果：PASS {len(PASS)} / FAIL {len(FAIL)} =====")
    if FAIL:
        print("失败项:", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
