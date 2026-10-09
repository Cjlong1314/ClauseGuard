"""Pydantic数据模型：合同、条款、审查结果"""
from typing import List, Optional
from pydantic import BaseModel, Field
from datetime import datetime


class Clause(BaseModel):
    """条款树节点"""
    clause_id: str = Field(..., description="条款编号，如3.2")
    title: str = Field("", description="条款标题")
    content: str = Field(..., description="条款正文")
    level: int = Field(1, description="层级")
    parent: Optional[str] = Field(None, description="父条款编号")


class Party(BaseModel):
    role: str  # 甲方/乙方/丙方
    name: str
    identifier: str = ""  # 统一社会信用代码


class ExtractedInfo(BaseModel):
    """要素抽取结果"""
    contract_type: str = "未知"
    parties: List[Party] = []
    amount: str = ""
    term: str = ""          # 履行期限
    sign_date: str = ""     # 签署日期
    jurisdiction: str = ""          # 管辖法院/仲裁机构
    dispute_resolution: str = ""    # 争议解决方式：诉讼/仲裁


class ExtractionConflict(BaseModel):
    """正则与LLM交叉校验冲突项"""
    field: str
    regex_value: str = ""
    llm_value: str = ""
    status: str = "需人工确认"


class ExtractionResult(BaseModel):
    """F6要素抽取结果：正则基线+LLM通道+交叉校验"""
    contract_id: str
    extracted: ExtractedInfo                  # 合并后的最终结果
    regex_extracted: ExtractedInfo            # 正则基线
    llm_extracted: Optional[ExtractedInfo] = None  # LLM结果，未配置/失败为None
    conflicts: List[ExtractionConflict] = []  # 冲突项（需人工确认）
    source: str = "regex"                     # regex/llm_merged
    llm_status: str = "disabled"              # disabled/ok/failed
    llm_error: str = ""                       # LLM失败原因（供排查）
    created_at: str = ""


class ExtractionCorrection(BaseModel):
    """人工修正记录"""
    contract_id: str
    corrected: ExtractedInfo
    origin: Optional[ExtractedInfo] = None
    operator: str = ""
    comment: str = ""
    time: str = ""


class ReviewFinding(BaseModel):
    """单条审查结果"""
    finding_id: str
    dimension: str       # 违法违规/缺失条款/风险条款/格式规范
    risk_level: str      # 高/中/低
    clause_id: str = ""  # 涉及条款，缺失类为空
    excerpt: str = ""    # 命中原文片段
    rule_name: str = ""
    legal_basis: str = ""     # 法条原文
    suggestion: str = ""      # 修改建议
    status: str = Field("open", description="处理状态: open/confirmed/rejected/ignored")
    comment: str = Field("", description="人工处理意见")
    ai_conclusion: str = Field("", description="AI研判结论")
    recalled_laws: list = Field([], description="RAG召回的法条")


class ReviewResult(BaseModel):
    contract_id: str
    reviewed_at: str
    findings: List[ReviewFinding] = []
    summary: dict = {}   # 按等级/维度统计


class Contract(BaseModel):
    contract_id: str
    filename: str
    upload_time: str
    raw_text: str = ""
    clauses: List[Clause] = []
    extracted: Optional[ExtractedInfo] = None
    review: Optional[ReviewResult] = None
