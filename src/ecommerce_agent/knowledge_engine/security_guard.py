"""知识引擎安全护栏：检索链路的注入检测 / 不可信边界 / 敏感信息防护。

设计（对齐项目安全基线，与 policy.py 客服入口互补）：
- policy.py 管"客服入口"的 prompt 注入检测 + 未授权数据请求拒绝。
- 本模块管"知识引擎检索链路"自身：
  ① 检索内容注入检测：知识库里被污染的条目（如含恶意指令）不能进入客服回答
  ② 检索结果不可信化：标记检索结果边界，禁止模型把知识内容当指令执行
  ③ 敏感信息扫描：检索结果不携带明文 PII / 密钥 / 内部标识
  ④ 任务摄取拒绝：超范围请求（竞品数据、隐私、内部信息）在检索前显式拒绝/升级

纯标准库、零第三方依赖，与 knowledge_engine 其它模块一致。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# ---------- ① 注入检测 ----------

# 归一化辅助：清除零宽字符 / 控制字符，防字符级绕过
_ZERO_WIDTH_RE = re.compile(r"[​‌‍⁠﻿‎‏]")
_INJECTION_PREFIXES = ("忽略", "无视", "忘记", "不要理", "不用管", "别管", "抛开", "清除", "清空")
_INJECTION_SUBJECTS = (
    "系统", "之前", "以上", "先前", "前文", "历史", "所有", "全部", "你的",
    "上面的", "上述", "既定", "原", "预设", "原始",
)
_INJECTION_NOUNS = ("提示", "提示词", "指令", "规则", "要求", "设定", "配置", "约束", "上下文", "prompt", "instructions", "规则集")


def _normalize_for_scan(text: str) -> str:
    """归一化待扫描文本：去零宽/控制字符 + 全小写，防字符级注入绕过。"""
    out = _ZERO_WIDTH_RE.sub("", text or "")
    return out.lower()


def _build_injection_patterns() -> list[re.Pattern[str]]:
    """构造注入检测正则集合（含变体容忍，覆盖工作流复测发现的绕过向量）。"""
    pats: list[re.Pattern[str]] = []

    # 1. 中文"忽略/无视 + 任意间隙 + 提示/指令/规则"（容忍插入词）
    #    注意：不含"忘记"（"忘记密码/忘记账号"是正常客服内容，非注入意图）
    gap = r"[\s\S]{0,4}"  # 允许 0-4 个任意字符插入（含零宽/插入词）
    for prefix in ("忽略", "无视", "不要理", "不用管", "别管", "抛开", "清除", "清空", "please ignore", "forget", "ignore", "disregard", "bypass"):
        # 中文组合：前缀 + (可选 subject) + 间隙 + 名词
        for subject in _INJECTION_SUBJECTS:
            pats.append(re.compile(rf"{prefix}\s*{gap}?{subject}?\s*{gap}?{_INJECTION_NOUNS[0]}(?:词)?", re.IGNORECASE))
        # 英文组合：ignore/disregard/forget all/the/any prior/previous/system/above + instructions/prompts/guidance
        for subject in ("all", "the", "any", "prior", "previous", "system", "above", "earlier"):
            pats.append(re.compile(rf"{prefix}\s+{subject}\s+(?:instructions?|prompts?|guidance|rules?)", re.IGNORECASE))
        # 通用名词后缀（无需 subject）：忽略/无视 + 提示词/指令/规则
        for noun in ("提示词", "指令", "规则", "要求", "设定", "提示"):
            pats.append(re.compile(rf"{prefix}\s*{gap}?{noun}", re.IGNORECASE))

    # 2. "扮演/假装/你现在是 + 系统/管理员"（含变体）
    for v in ("扮演", "假装", "模拟", "当你", "你现在是", "act as", "you are now", "pretend to be", "imagine you are"):
        for role in ("系统", "管理员", "客服", "老板", "另一个", "the system", "admin", "another"):
            pats.append(re.compile(rf"{v}\s*{gap}?{role}", re.IGNORECASE))

    # 3. "不要告诉/不能告诉/别泄露 + 系统内部对象"（含 don't tell / do not tell / do not follow）
    #    R3 修复：必须面向"系统/指令/秘密"等内部对象才算注入。KB 里"不要告诉客户账号密码/请勿泄露客户信息"
    #    是客服规范内容（对象=客户/他人），不是注入，必须放行。
    _LEAK_INTERNAL = r"(?:系统|指令|规则|提示|提示词|秘密|机密|内部|prompt|instruction|guidance|secret|internal|api\s*key|密码)"
    pats.append(re.compile(r"(?:不要|不能|别|不可|禁止|请勿|切勿)\s*(?:告诉|说出|透露|泄露|展示)\s*(?:这个|那个|上述)?\s*" + _LEAK_INTERNAL, re.IGNORECASE))
    pats.append(re.compile(r"do\s+n'?o?t\s+(?:tell|reveal|expose|show|follow)", re.IGNORECASE))
    pats.append(re.compile(r"don'?t\s+(?:tell|reveal|expose|show|follow)", re.IGNORECASE))
    pats.append(re.compile(r"do\s+not\s+(?:tell|reveal|expose|show|follow)", re.IGNORECASE))
    pats.append(re.compile(r"do\s+not\s+follow\s+(?:the\s+)?(?:system\s+)?(?:prompt|instructions?|guidance|rules?)", re.IGNORECASE))
    pats.append(re.compile(r"ignore\s+(?:the|all|any)\s+(?:system\s+)?(?:prompt\s+above|above|prior|previous|earlier)?\s*(?:instructions?|prompts?|guidance)", re.IGNORECASE))

    # 4. 泄露系统信息（R3 修复：内部对象后缀必须出现，禁止空后缀误伤"请勿泄露验证码给他人"）
    pats.append(re.compile(r"泄露\s*(?:系统|内部|机密|api|密钥|secret|密码|指令|提示|prompt|提示词)", re.IGNORECASE))
    pats.append(re.compile(r"reveal\s+(?:the\s+)?(?:system|prompt|secret|api\s*key)", re.IGNORECASE))
    # R3 补：面向"系统提示/指令/规则"的获取/遵循动作（攻击者提取 prompt 的变体），不误伤 KB 客服话术
    pats.append(re.compile(r"(?:把|将|请|给我)?\s*(?:系统提示|系统指令|提示词|prompt|instructions?)\s*(?:内容|全文)?\s*(?:告诉|给我|输出|发|发出来|讲|背出|念|发一下)", re.IGNORECASE))
    pats.append(re.compile(r"(?:不要|别|请勿|切勿)\s*(?:遵循|遵从|执行|照做)\s*(?:系统提示|系统指令|system\s+prompt|instructions?)", re.IGNORECASE))

    # 5. 越权获取内部数据（容忍"帮我查一下所有订单"等插入词）
    for prefix in ("查询", "查", "查看", "调出", "输出", "列出", "返回", "给我", "show", "give me", "select"):
        for target in ("所有订单", "全部订单", "所有用户", "全部用户", "所有买家", "全部买家", "数据库", "后台", "系统内部", "all orders", "all users", "database"):
            pats.append(re.compile(rf"{prefix}\s*{gap}?{target}", re.IGNORECASE))

    # 6. SQL 注入特征
    pats.append(re.compile(r"\bselect\b.{0,30}\bfrom\b\s+\w+", re.IGNORECASE))
    pats.append(re.compile(r"\b(drop\s+table|delete\s+from|insert\s+into|update\s+\w+\s+set)\b", re.IGNORECASE))

    return pats


_EMBEDDED_INSTRUCTION_PATTERNS = _build_injection_patterns()

# ② 敏感信息扫描：检索结果不得携带的明文数据
_SENSITIVE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"1[3-9]\d{9}"),                              # 手机号（连续）
    re.compile(r"\d{17}[\dXx]"),                              # 身份证号（连续）
    re.compile(r"(?i)(?:api[_-]?key|secret|password|token)\s*[:=：]", ),  # 密钥（容忍全角冒号）
    re.compile(r"(?i)sk-[A-Za-z0-9]{20,}"),                   # OpenAI/GLM 风格密钥
    re.compile(r"\b[0-9]{13,16}\b"),                          # 银行卡/长数字（连续）
    re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"),  # 邮箱
]

# R4 修复：业务上下文数字（条码/商品号/订单号）不算银行卡/身份证
# 13 位数字前面若紧跟"条码/商品号/货号/EAN/订单号"等业务词 → 是 EAN 条码，非卡号
# R4 修复：支持"订单号是 123..."/"订单号为 123..."/"参考号: 6222..."等带"是/为"分隔
_EAN_CONTEXT_RE = re.compile(r"(?:条码|商品号|货号|商品编码|ean|订单号|参考号|sku|序号|规格号|单号|编号)\s*(?:是|为|:)?\s*[:：]?\s*\d{8,}", re.IGNORECASE)
_EAN_BIZ_WORDS = r"条码|商品号|货号|商品编码|ean|订单号|参考号|sku|序号|规格号|单号|编号"


def _is_business_number(text: str, m: re.Match) -> bool:
    """判断一个数字匹配是否属于业务上下文（EAN 条码/订单号），而非 PII。

    R4 修复：前缀窗口覆盖"订单号是 123..."/"订单号为 123..."（含"是/为"分隔）。
    """
    # 检查数字前的 16 个字符是否含业务词（覆盖"订单号是 123..."等带"是/为/："前缀）
    start = max(0, m.start() - 16)
    prefix = text[start:m.start()]
    if re.search(_EAN_BIZ_WORDS, prefix, re.IGNORECASE):
        return True
    return False

# 带分隔符的号码归一化辅助：识别"手机 138 1234 5678"等（A4 修复：分隔符绕过）
_DIGIT_SEP_RE = re.compile(r"[0-9](?:[-\s.]*[0-9]){5,}")  # 至少 6 位数字，容忍分隔符
_PHONE_BOUNDARY_RE = re.compile(r"(?<!\d)1[3-9][0-9\-.\s]{9,13}(?!\d)")
# R5 修复：银行卡/长号码不用"点"分隔（卡号是连续或空格/连字符分隔），排除"."避免误伤版本号 1.2.3.4
_CARD_BOUNDARY_RE = re.compile(r"(?<!\d)[0-9][0-9\-\s]{11,17}(?!\d)")
# 固定电话：区号(0xx/0xxx) + 7-8 位号码，带分隔符（"座机 010-12345678"）
_LANDLINE_BOUNDARY_RE = re.compile(r"(?<!\d)0\d{2,3}[-.\s]\d{7,8}(?!\d)")


def _digits_only(text: str) -> str:
    """只保留数字，用于带分隔符号码的二次校验。"""
    return "".join(ch for ch in text if ch.isdigit())


def _looks_like_separated_phone(text: str) -> bool:
    """判断文本是否含带分隔符的手机号（11 位，1[3-9] 开头）。"""
    for m in _DIGIT_SEP_RE.finditer(text):
        digits = _digits_only(m.group())
        if len(digits) == 11 and digits.startswith("1") and digits[1] in "3456789":
            return True
    return False


def _looks_like_separated_card(text: str) -> bool:
    """判断文本是否含带分隔符的银行卡/长号码（13-19 位数字）。"""
    for m in _DIGIT_SEP_RE.finditer(text):
        digits = _digits_only(m.group())
        if 13 <= len(digits) <= 19:
            return True
    return False


def _looks_like_separated_id(text: str) -> bool:
    """判断文本是否含带分隔符的身份证号（17 位数字 + 可选 X）。"""
    for m in _DIGIT_SEP_RE.finditer(text):
        digits = _digits_only(m.group())
        if len(digits) == 17:
            return True
    return False


@dataclass
class GuardDecision:
    """安全护栏判定结果。"""

    allowed: bool
    reason: str = ""
    detail: str = ""
    action: str = "allow"  # allow / block / sanitize / escalate

    @classmethod
    def allow(cls) -> "GuardDecision":
        return cls(allowed=True, reason="", action="allow")

    @classmethod
    def block(cls, reason: str, detail: str = "", action: str = "block") -> "GuardDecision":
        return cls(allowed=False, reason=reason, detail=detail, action=action)


@dataclass
class GuardResult:
    """一条检索结果的安全处理结果。"""

    item: dict[str, Any]
    status: str = "clean"        # clean / injected / sensitive / sanitized
    reason: str = ""
    matched: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "reason": self.reason, "matched": self.matched}


class KnowledgeSecurityGuard:
    """知识引擎检索链路安全护栏。"""

    def __init__(self) -> None:
        self._injection_patterns = _EMBEDDED_INSTRUCTION_PATTERNS
        self._sensitive_patterns = _SENSITIVE_PATTERNS

    # ---------- ① 注入检测 ----------

    def scan_injection(self, text: str) -> list[str]:
        """扫描文本是否含注入特征，返回命中的模式。

        归一化后匹配（去零宽/控制字符 + 小写），防字符级绕过。
        """
        if not text:
            return []
        norm = _normalize_for_scan(text)
        hits: list[str] = []
        for pat in self._injection_patterns:
            m = pat.search(norm)
            if m:
                hits.append(pat.pattern[:60])
        return hits

    # ---------- ② 敏感信息扫描 ----------

    def scan_sensitive(self, text: str) -> list[str]:
        """扫描文本是否含敏感明文，返回命中的模式。

        A4 修复：除连续数字匹配外，额外识别带分隔符的手机号/银行卡/身份证
        （如"手机 138 1234 5678"、"6222 0200 0000 0000"）。
        R3 修复：EAN 条码/商品号/订单号等业务数字不误判为卡号/身份证。
        """
        if not text:
            return []
        hits: list[str] = []
        for pat in self._sensitive_patterns:
            m = pat.search(text)
            if m:
                # R3：13-16 位数字若属业务上下文（条码/商品号），不算银行卡
                if pat.pattern in (r"\b[0-9]{13,16}\b", r"\d{17}[\dXx]") and _is_business_number(text, m):
                    continue
                hits.append(pat.pattern[:60])
        # 带分隔符号码识别（同样排除业务上下文）
        if _looks_like_separated_phone(text):
            hits.append("separated-phone")
        if _looks_like_separated_card(text) and not _EAN_CONTEXT_RE.search(text):
            hits.append("separated-bankcard")
        if _looks_like_separated_id(text) and not _EAN_CONTEXT_RE.search(text):
            hits.append("separated-idcard")
        if _LANDLINE_BOUNDARY_RE.search(text):
            hits.append("landline")
        return hits

    # ---------- ③ 任务摄取拒绝（检索前意图门） ----------

    _OUT_OF_SCOPE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
        ("competitor_data", re.compile(r"竞品|同行|对家|竞对|竞争对手|别家|友商|对手店", re.IGNORECASE)),
        ("competitor_data", re.compile(r"(?:他们|对方|别家|友商).{0,6}(?:销量|价格|库存|报价)", re.IGNORECASE)),
        # 他人隐私：必须限定"别人/另一个/某个用户"的所有格 + 敏感数据（放行"我的/自己的"）
        ("private_data", re.compile(r"(?:别人的|另一个|其他(?:人|用户|顾客|买家|客户)?的|某个(?:用户|顾客|买家|客户)的|那位(?:顾客|买家|用户)?的)(?:手机号|电话|手机|号码|地址|订单|信息|身份证)", re.IGNORECASE)),
        ("private_data", re.compile(r"(?:查|查一下|给我|告诉我|说下|找找|调出)(?:下|一下)?(?:所有|全部|任意|每个|另一个|别的)(?:用户|顾客|买家|客户)?(?:的)?(?:手机号|电话|地址|身份证|订单|订单信息|订单列表)", re.IGNORECASE)),
        # 批量拉取所有格：所有/全部 + 用户类 + 的 + 敏感数据（"所有用户的手机号给我"）——放行"所有用户都享 X 政策"类无"的+敏感数据"形态
        ("private_data", re.compile(r"(?:所有|全部|任意|每个)(?:用户|顾客|买家|客户)的(?:手机号|电话|地址|身份证|银行卡|订单|信息)", re.IGNORECASE)),
        ("private_data", re.compile(r"(?:查|给我|列出|调出)(?:下|一下)?(?:所有|全部|任意|每一个|所有用户)(?:订单|用户|客户|买家|数据|库存)", re.IGNORECASE)),
        ("private_data", re.compile(r"(?:他的|她的|那个人的|这位(?:顾客|买家)?的)(?:手机号|电话|地址|订单|信息|身份证)", re.IGNORECASE)),
        # 只拦截"他人的X手机号/电话是多少"，放行"我的/自己的"（R2 修复）
        ("private_data", re.compile(r"(?:别人的|他的|她的|那个人的|某位顾客的|那个顾客的|那个买家的|某人的)[^。，；\s]{0,6}(?:手机号|电话号码|联系方式|电话)是", re.IGNORECASE)),
        # 姓名 + 手机号/电话 组合（"张三的手机号是多少"）。捕获姓名段，由 classify_request 代码排除自指代词。
        # 正则只负责识别形态，自指判定放代码（负向后顾无法覆盖"我的手机号"在行首等情形）。
        ("private_data", re.compile(r"([^\s，。；：:]{1,4})(?:的)?(?:手机号|电话号码|电话|联系方式)(?:是多少|是什么|是几)", re.IGNORECASE)),
        # 内部数据：必须含"查/看/访问"动作 + 内部对象（放行"老板在吗"类问候）
        ("internal_data", re.compile(r"(?:查|看|查看|访问|进入|读取|导出)(?:下|一下)?(?:内部|后台|数据库|系统日志|管理层数据)", re.IGNORECASE)),
        ("internal_data", re.compile(r"(?:内部|后台|数据库)(?:的)?(?:数据|信息|记录|报表|密码)", re.IGNORECASE)),
        # 账户密码窃取：必须含"别人的/某人的"或"查/给/盗取"动作（放行"忘记密码/重置密码"类合法求助）
        ("forbidden_entity", re.compile(r"(?:别人的|他的|她的|某个(?:用户|账号)?的)(?:密码|验证码|账号|卡号)", re.IGNORECASE)),
        ("forbidden_entity", re.compile(r"(?:查|告诉我|给我|获取|盗)(?:一下)?(?:别人的|他的|她的|用户的|某人的)?(?:密码|验证码|登录账号|银行卡)", re.IGNORECASE)),
        ("forbidden_entity", re.compile(r"(?:身份证号|银行卡号|卡号)是多少|查(?:一下)?(?:身份证|银行卡)", re.IGNORECASE)),
        ("legal_boundary", re.compile(r"违法|走私|假货|仿冒|刷单|绕过.{0,6}(?:审核|监管)|黑产|诈骗", re.IGNORECASE)),
    ]

    # R2 修复：自指代词（我的/自己的/本人/俺）不作为"他人姓名"拦截。
    # 姓名+手机号模式捕获姓名段后，此集合内视为自指，放行。
    # 注意：他/她/那/这是"他人/对方"，不属于自指，必须拦截。
    _SELF_REFERENTIAL = frozenset("我自分本俺")

    def classify_request(self, query: str) -> GuardDecision:
        """检索前意图门：识别超范围请求并拒绝/升级。

        返回 GuardDecision：
        - allow：正常业务问题，放行检索
        - block + action=escalate：越权/敏感请求，升级人工
        - block + action=block：明确违规/拒绝
        """
        if not query or not query.strip():
            return GuardDecision.allow()
        q = query.strip()
        hits: list[str] = []
        escalate = False
        for label, pat in self._OUT_OF_SCOPE_PATTERNS:
            blocked = False
            if pat.groups:
                # 带捕获组的姓名+手机号模式：捕获姓名段，自指代词放行
                for m in pat.finditer(q):
                    name = (m.group(1) or "").strip()
                    if name and any(ch in self._SELF_REFERENTIAL for ch in name):
                        continue  # 自指：放行
                    blocked = True
                    break
            else:
                blocked = bool(pat.search(q))
            if blocked:
                hits.append(label)
                if label == "competitor_data":
                    escalate = True
        if hits:
            action = "escalate" if escalate else "block"
            # 投诉语境降级：legal_boundary 命中"假货/诈骗/刷单"等词时，
            # 若为"受害/举报"句式（收到/买到/我被/我要投诉/遇到），降为 escalate（转人工），
            # 而不是 block（拒绝）——消费者投诉是最核心的客服场景，不能直接拒绝。
            if (
                action == "block"
                and "legal_boundary" in hits
                and re.search(
                    r"收到|买到了|买到假货|我被|我是受害者|遇到|我要投诉|投诉退款|退货|举报|上当受骗|被骗了",
                    q,
                )
            ):
                action = "escalate"
            return GuardDecision.block(
                reason="out_of_scope_request",
                detail=f"命中超范围请求类别: {', '.join(hits)}",
                action=action,
            )
        return GuardDecision.allow()

    # ---------- 检索结果处理 ----------

    def sanitize_text(self, text: str) -> str:
        """对检索结果做基础脱敏（手机号/身份证打码），保留下游可用。

        A4 修复：额外处理带分隔符的号码（空格/连字符/点分隔）。
        R3 修复：邮箱脱敏 + EAN/订单号业务数字不打码。
        """
        if not text:
            return text
        out = text
        # 连续手机号 / 身份证
        out = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", r"1**********", out)
        # R3：身份证/银行卡打码需排除业务上下文（EAN 条码/订单号）
        out = re.sub(
            r"(?<!\d)\d{17}[\dXx](?!\d)",
            lambda m: m.group(0) if _is_business_number(out, m) else "*" * len(m.group(0)),
            out,
        )
        out = re.sub(
            r"(?<!\d)\d{13,16}(?!\d)",
            lambda m: m.group(0) if _is_business_number(out, m) else "*" * len(m.group(0)),
            out,
        )
        # 带分隔符的手机号：如 "138 1234 5678" / "138-1234-5678" → 整段打码
        out = _PHONE_BOUNDARY_RE.sub(lambda m: "1**********", out)
        # 带分隔符的银行卡/长号码：如 "6222 0200 0000 0000" → 整段打码（排除业务上下文）
        out = _CARD_BOUNDARY_RE.sub(
            lambda m: m.group(0) if _EAN_CONTEXT_RE.search(out[max(0, m.start()-6):m.end()]) else "*" * min(len(_digits_only(m.group())), 16),
            out,
        )
        # 固定电话：如 "010-12345678" → 打码
        out = _LANDLINE_BOUNDARY_RE.sub(lambda m: "0" + "*" * (len(_digits_only(m.group())) - 1), out)
        # R3：邮箱脱敏（保留本地部分首字符，域部分打码）
        out = re.sub(
            r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
            lambda m: m.group(0)[:1] + "***@" + m.group(0).split("@")[1][:1] + "***",
            out,
        )
        return out

    def inspect_item(self, item: dict[str, Any]) -> GuardResult:
        """检查一条检索结果：注入 / 敏感 / 正常。

        返回 GuardResult，调用方决定丢弃或脱敏。
        """
        # 拼接该条目的全部文本字段供扫描
        text_fields = [
            str(item.get(k) or "") for k in
            ("question", "answer", "title", "content", "policy_name",
             "canonical_answer", "keywords", "rule_title", "content_summary")
        ]
        joined = "\n".join(text_fields)

        inj = self.scan_injection(joined)
        if inj:
            return GuardResult(item, status="injected", reason="embedded_instruction_detected", matched=inj)

        sens = self.scan_sensitive(joined)
        if sens:
            # 敏感信息：尝试脱敏后保留，若无法脱敏则标记
            sanitized = False
            for key in ("answer", "content", "question", "canonical_answer"):
                if item.get(key):
                    cleaned = self.sanitize_text(str(item[key]))
                    if cleaned != str(item[key]):
                        item[key] = cleaned
                        sanitized = True
            if sanitized:
                return GuardResult(item, status="sanitized", reason="pii_redacted", matched=sens)
            return GuardResult(item, status="sensitive", reason="pii_detected", matched=sens)

        return GuardResult(item, status="clean")


# 便捷入口：单例
_default_guard: KnowledgeSecurityGuard | None = None


def get_security_guard() -> KnowledgeSecurityGuard:
    """获取全局安全护栏实例（懒加载单例）。"""
    global _default_guard
    if _default_guard is None:
        _default_guard = KnowledgeSecurityGuard()
    return _default_guard
