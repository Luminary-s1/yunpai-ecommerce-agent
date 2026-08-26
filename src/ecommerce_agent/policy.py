from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from .text_utils import normalize_text, redact_sensitive


PROMPT_INJECTION_PATTERNS = (
    r"忽略.{0,8}(之前|以上|系统).{0,8}(指令|规则)",
    r"(system prompt|系统提示词|开发者消息|隐藏指令)",
    r"(越权|绕过).{0,8}(限制|权限|审核|平台)",
)
PROMPT_DISCLOSURE_ACTION_PATTERN = re.compile(
    r"(输出|打印|显示|展示|复述|重复|回显|泄露|贴出|逐字|原样|一字不差|"
    r"print|show|reveal|repeat|recite|display|dump|quote|verbatim|word\s+for\s+word)",
    re.IGNORECASE,
)
PROMPT_DISCLOSURE_TARGET_PATTERN = re.compile(
    r"((?:system|系统).{0,6}(?:prompt|提示|消息|指令|规则|设定)|"
    r"内部.{0,6}(提示|消息|指令|规则|设定)|"
    r"开发者.{0,6}(消息|指令|规则|模式)|隐藏.{0,6}(消息|指令|规则|设定)|"
    r"角色.{0,6}(设定|规则|文字)|设定.{0,6}角色|"
    r"developer\s+(message|instruction|mode)|"
    r"hidden\s+(prompt|policy|instruction|rule)|internal\s+(prompt|policy|instruction|rule))",
    re.IGNORECASE,
)

HIGH_RISK_ACTION_PATTERNS = (
    r"(帮我|给我|立即|马上|现在).{0,6}(退款|退钱|赔付|赔偿|补偿)",
    r"(申请|执行|操作).{0,5}(退款|退货|换货|赔付)",
    r"(改|修改|换).{0,5}(价格|价钱|地址|手机号|收货人|收件人|发票抬头)",
    r"(价格|价钱|地址|手机号|收货人|收件人|发票抬头).{0,8}(改|修改|换)",
    r"(取消|关闭|删除).{0,4}订单",
    r"(补发|重新发|拦截快递|召回包裹)",
    r"(添加|增加|修改|更新).{0,5}(订单)?(备注|留言)",
    r"(补发|发放).{0,4}(优惠券|券)",
    r"(确认收货|延长收货)",
)

UNAUTHORIZED_DATA_PATTERNS = (
    r"(别家|竞品|其他店铺).{0,8}(真实销量|库存|订单|买家)",
    r"(其他|别的).{0,5}(买家|客户).{0,5}(电话|地址|数据|信息)",
)

_ACTION_ACTOR = r"(?:我|我们|客服|这边|系统|本店)"
_ACTION_BENEFICIARY = r"(?:(?:为|给|帮|替)(?:您|你))"
_ACTION_ALREADY = r"(?:已经|已|现已)"
_ACTION_OPERATION = (
    r"(?:办理|办|申请|提交|执行|操作|处理|安排|搞定)"
    r"(?:好|妥|完|完成|成功)?(?:了)?"
)
_ACTION_COMPLETION = rf"(?:{_ACTION_OPERATION}|完成(?:了)?|成功(?:了)?)"
_ACTION_COMPLETION_BEFORE = (
    rf"(?:{_ACTION_ACTOR}.{{0,4}}(?:{_ACTION_ALREADY}|{_ACTION_BENEFICIARY})"
    rf".{{0,6}}(?:{_ACTION_COMPLETION}.{{0,4}})?|"
    rf"{_ACTION_ALREADY}.{{0,4}}{_ACTION_BENEFICIARY}.{{0,4}}"
    rf"(?:{_ACTION_COMPLETION}.{{0,4}})?|"
    rf"{_ACTION_ALREADY}.{{0,4}}{_ACTION_COMPLETION}.{{0,4}}|"
    rf"{_ACTION_BENEFICIARY}.{{0,4}}(?:{_ACTION_COMPLETION}.{{0,4}})?)"
)
_ACTION_COMPLETION_AFTER = (
    rf"(?:(?:{_ACTION_ACTOR}|{_ACTION_BENEFICIARY}).{{0,4}}"
    rf"(?:{_ACTION_ALREADY}.{{0,4}})?(?:{_ACTION_BENEFICIARY}.{{0,4}})?"
    rf"{_ACTION_COMPLETION}|"
    rf"{_ACTION_ALREADY}.{{0,4}}{_ACTION_OPERATION}|"
    rf"{_ACTION_ALREADY}.{{0,4}}(?:{_ACTION_ACTOR}|{_ACTION_BENEFICIARY})"
    rf".{{0,4}}(?:{_ACTION_COMPLETION})?)"
)


def _action_claim_pattern(action_terms: str) -> re.Pattern[str]:
    return re.compile(
        rf"(?:{_ACTION_COMPLETION_BEFORE}.{{0,10}}(?:{action_terms})|"
        rf"(?:{action_terms}).{{0,10}}{_ACTION_COMPLETION_AFTER})"
    )


def _extend_action_claim_pattern(
    base: re.Pattern[str],
    *extra_patterns: str,
) -> re.Pattern[str]:
    return re.compile("|".join((base.pattern, *extra_patterns)))


_BUSINESS_ACTION_OUTPUT_CLAIMS = (
    (
        "refund",
        _extend_action_claim_pattern(
            _action_claim_pattern(r"退款|退钱"),
            rf"{_ACTION_ACTOR}.{{0,4}}(?:把)?(?:退款|退钱).{{0,8}}"
            r"(?:办|办理|申请|提交|处理|退)(?:好|妥|完|完成|成功|回)?(?:了)?",
            rf"(?:退款|退钱).{{0,6}}{_ACTION_ACTOR}.{{0,4}}"
            rf"(?:{_ACTION_ALREADY}.{{0,4}})?"
            r"(?:办|办理|申请|提交|处理|退)(?:好|妥|完|完成|成功|回)?(?:了)?",
            rf"{_ACTION_BENEFICIARY}.{{0,4}}(?:原路)?退(?:回|给)?(?:去)?(?:了)|"
            r"(?:钱|款项).{0,6}(?:已|已经)?(?:原路)?(?:打回|退回)(?:去)?(?:了)|"
            r"(?:钱|款|款项).{0,4}退(?:给)?(?:您|你)(?:了)?|"
            r"(?:退款|退钱).{0,6}(?:给|为)(?:您|你).{0,6}(?:原路)?退(?:回|给)?"
            r"(?:去)?(?:了)",
        ),
        frozenset({"refund_order"}),
    ),
    (
        "return",
        _action_claim_pattern(r"退货"),
        frozenset({"return_order"}),
    ),
    (
        "exchange",
        _extend_action_claim_pattern(
            _action_claim_pattern(r"换货"),
            rf"(?:{_ACTION_ALREADY}.{{0,4}})?{_ACTION_BENEFICIARY}.{{0,4}}"
            r"换(?:成|为)?(?:新(?:的|货|款)?|一件)(?:了)?",
        ),
        frozenset({"exchange_order"}),
    ),
    (
        "compensation",
        _extend_action_claim_pattern(
            _action_claim_pattern(r"赔付|赔偿|补偿"),
            r"(?:赔付|赔偿|补偿).{0,6}(?:已|已经)"
            r"(?:完成|支付|发放)(?:成功|完成|了)?",
        ),
        frozenset({"compensate_order"}),
    ),
    (
        "cancel_order",
        _extend_action_claim_pattern(
            _action_claim_pattern(
                r"(?:取消|撤销|关闭|关掉|作废)(?:这个)?订单|"
                r"订单(?:取消|撤销|关闭|关掉|作废)"
            ),
            rf"订单.{{0,6}}(?:{_ACTION_ALREADY}.{{0,4}})?"
            rf"{_ACTION_BENEFICIARY}.{{0,4}}"
            r"(?:取消|撤销|撤了|关闭|关掉|作废)"
            r"(?:好|妥|完成|成功)?(?:了)?",
            rf"{_ACTION_ACTOR}.{{0,4}}(?:把)?订单.{{0,6}}"
            r"(?:取消|撤销|撤了|关闭|关掉|作废)"
            r"(?:好|妥|完|完成|成功)?(?:了)?",
            rf"订单.{{0,6}}{_ACTION_ACTOR}.{{0,4}}"
            rf"(?:{_ACTION_ALREADY}.{{0,4}})?"
            r"(?:取消|撤销|撤了|关闭|关掉|作废)"
            r"(?:好|妥|完|完成|成功)?(?:了)?",
            r"(?:这单|该单|单子).{0,6}(?:已|已经)?"
            r"(?:撤|取消|关闭|关|作废)(?:掉|成功|完成|了)?",
        ),
        frozenset({"cancel_order", "order_cancel"}),
    ),
    (
        "modify_order",
        _extend_action_claim_pattern(
            _action_claim_pattern(r"修改订单|改订单|更新订单|变更订单|调整订单"),
            r"订单(?![^，。；]{0,4}地址).{0,6}(?:(?:已|已经)(?:修改|更改|更新)"
            r"(?:好|妥|完|完成|成功|了)?|"
            r"(?:修改|更改|更新)(?:好|妥|完|完成|成功|了))",
        ),
        frozenset({"update_order", "modify_order", "change_order", "order_update"}),
    ),
    (
        "change_address",
        _extend_action_claim_pattern(
            _action_claim_pattern(
                r"(?:修改|改|更新|变更|调整)(?:收货)?地址|"
                r"(?:修改|更新|变更|调整)收货信息"
            ),
            rf"(?:收货)?地址.{{0,6}}(?:{_ACTION_ALREADY}).{{0,4}}"
            rf"(?:{_ACTION_BENEFICIARY}.{{0,4}})?"
            r"(?:改|修改|更改|更新|变更|调整)(?:好|妥|完|完成|成功|了)?|"
            rf"(?:收货)?地址.{{0,6}}(?:{_ACTION_BENEFICIARY}.{{0,4}})?"
            r"(?:改|修改|更改|更新|变更|调整)(?:好|妥|完|完成|成功|了)",
            r"(?:已|已经).{0,4}(?:把|将)?(?:收货)?地址.{0,6}"
            r"(?:改|修改|更改|更新|变更|调整)(?:成|为).{0,8}(?:了)",
        ),
        frozenset({"update_order_address", "change_order_address"}),
    ),
    (
        "reship",
        _extend_action_claim_pattern(
            _action_claim_pattern(r"补发|重新发|重发|重新寄|再寄"),
            rf"(?:补发|重新发|重发|重新寄|再寄).{{0,6}}{_ACTION_ACTOR}.{{0,4}}"
            rf"(?:{_ACTION_ALREADY}).{{0,4}}"
            r"安排(?:好|妥|完|完成|成功|了)?|"
            rf"(?:补发|重新发|重发|重新寄|再寄).{{0,6}}{_ACTION_ACTOR}.{{0,4}}"
            r"安排(?:好|妥|完|完成|成功|了)",
            rf"(?:{_ACTION_ALREADY}).{{0,4}}安排.{{0,4}}"
            r"(?:补发|重新发|重发|重新寄|再寄)",
            rf"(?:{_ACTION_ALREADY}).{{0,6}}(?:把|将)?"
            r"(?:商品|货物|货|包裹).{0,8}(?:重新|再次|再).{0,6}"
            rf"(?:{_ACTION_BENEFICIARY}.{{0,4}})?(?:寄出|发出|发走)(?:了)?",
        ),
        frozenset({"reship_order", "create_replacement_shipment"}),
    ),
    (
        "replacement_item_sent",
        re.compile(
            r"(?:(?:新货|新的|新件|替换件).{0,8}(?:已|已经)?"
            rf"(?:{_ACTION_BENEFICIARY}.{{0,4}})?(?:发出|寄出|发走)(?:了)?|"
            rf"(?:{_ACTION_ALREADY}).{{0,6}}(?:把|将)?"
            r"(?:新货|新的|新件|替换件).{0,8}"
            rf"(?:(?:{_ACTION_BENEFICIARY}.{{0,4}})?(?:发出|寄出|发走)|"
            r"(?:发|寄)(?:出|走)?(?:给|为)(?:您|你))(?:了)?)"
        ),
        frozenset({"exchange_order", "reship_order", "create_replacement_shipment"}),
    ),
    (
        "invoice",
        _extend_action_claim_pattern(
            _action_claim_pattern(r"开票|开发票"),
            rf"发票.{{0,6}}(?:{_ACTION_ALREADY}).{{0,4}}"
            rf"(?:{_ACTION_BENEFICIARY}.{{0,4}})?"
            r"(?:开|开具|开出)(?:好|妥|完|完成|成功|了)?|"
            rf"发票.{{0,6}}(?:{_ACTION_BENEFICIARY}.{{0,4}})?"
            r"(?:开|开具|开出)(?:好|妥|完|完成|成功|了)",
        ),
        frozenset({"create_invoice"}),
    ),
    (
        "change_price",
        _extend_action_claim_pattern(
            _action_claim_pattern(r"改价|修改价格|更改价格|调整价格|变更价格"),
            r"(?:价格|售价).{0,6}(?:已|已经)"
            r"(?:修改|更改|调整|变更)(?:好|妥|完|完成|成功|了)?",
        ),
        frozenset({"update_product_price", "change_price", "update_price"}),
    ),
    (
        "change_recipient",
        _extend_action_claim_pattern(
            _action_claim_pattern(
                r"(?:修改|更改|更新|变更|调整)(?:收货人|手机号|联系电话|联系方式)|"
                r"(?:收货人|收件人|手机号|联系电话|联系方式)"
                r"(?:修改|更改|更新|变更|调整)"
            ),
            r"(?:收货人|收件人|手机号|联系电话|联系方式).{0,6}"
            r"(?:(?:已|已经)(?:修改|更改|更新|变更|调整)"
            r"(?:好|妥|完|完成|成功|了)?|"
            r"(?:修改|更改|更新|变更|调整)(?:完成|成功|完毕|好了))",
            rf"(?:收货人|收件人|手机号|联系电话|联系方式).{{0,6}}"
            rf"(?:{_ACTION_ALREADY}).{{0,4}}(?:{_ACTION_BENEFICIARY}.{{0,4}})?"
            r"(?:改|修改|更改|更新|变更|调整|换)(?:好|妥|完|完成|成功|了)",
            r"(?:手机号|联系电话|联系方式|号码).{0,6}(?:已|已经)?"
            r"(?:改|修改|更改|更新|变更|调整|换)(?:成|为).{0,8}(?:了)",
        ),
        frozenset(
            {
                "update_order_contact",
                "update_order_recipient",
                "update_order_phone",
            }
        ),
    ),
    (
        "change_invoice_title",
        _extend_action_claim_pattern(
            _action_claim_pattern(
                r"(?:修改|更改|更新|变更|调整)发票抬头|"
                r"发票抬头(?:修改|更改|更新|变更|调整)"
            ),
            r"发票抬头.{0,6}(?:已|已经)"
            r"(?:修改|更改|更新|变更|调整)(?:好|妥|完|完成|成功|了)?",
            r"(?:发票)?抬头.{0,6}(?:已|已经)"
            r"(?:改|修改|更改|更新|变更|调整)(?:好|妥|完|完成|成功|了)?",
            r"(?:发票)?抬头.{0,6}(?:改|修改|更改|更新|变更|调整)"
            r"(?:成|为).{0,8}(?:了)",
        ),
        frozenset({"update_invoice_title", "update_order_invoice_title"}),
    ),
    (
        "shipment_control",
        _extend_action_claim_pattern(
            _action_claim_pattern(r"拦截(?:快递|包裹)|召回包裹|追回包裹"),
            r"(?:快递|包裹).{0,6}(?:已|已经)(?:拦截|召回|追回)"
            r"(?:成功|完成|了)?",
            r"(?:快递|包裹).{0,6}(?:已|已经)?"
            r"(?:拦下|截下)(?:来)?(?:成功|完成|了)?",
            rf"(?:快递|包裹).{{0,6}}(?:{_ACTION_ALREADY}).{{0,4}}"
            rf"(?:{_ACTION_BENEFICIARY}.{{0,4}})?"
            r"(?:拦下|截下|拦住|截住)(?:来)?(?:成功|完成|了)?",
        ),
        frozenset(
            {"intercept_shipment", "intercept_package", "recall_shipment"}
        ),
    ),
    (
        "expedite_shipment",
        _extend_action_claim_pattern(
            _action_claim_pattern(r"催发货|催促发货|加急发货|催促出库"),
            r"(?:催发货|催促发货|加急发货|催促出库).{0,6}"
            r"(?:已|已经)(?:提交|安排|处理)(?:成功|完成|了)?",
            rf"(?:{_ACTION_BENEFICIARY}.{{0,4}})?"
            r"(?:催|催促)(?:发货|出库)?(?:过|成功|完成|了)",
        ),
        frozenset({"expedite_order", "urge_shipment"}),
    ),
    (
        "order_note",
        _extend_action_claim_pattern(
            _action_claim_pattern(r"(?:添加|增加|修改|更新|变更)(?:订单)?(?:备注|留言)"),
            r"(?:订单)?(?:备注|留言).{0,6}(?:已|已经)"
            r"(?:添加|增加|修改|更新|变更)(?:完成|成功|完毕|了)?",
            r"(?:订单)?(?:备注|留言).{0,6}(?:已|已经)?"
            r"(?:添加|增加|加|写)(?:上|入|好)?(?:成功|完成|了)",
        ),
        frozenset({"add_order_note", "update_order_note"}),
    ),
    (
        "coupon",
        _extend_action_claim_pattern(
            _action_claim_pattern(r"(?:补发|发放|重新发)(?:优惠券|券)"),
            r"(?:优惠券|券).{0,6}(?:已|已经)(?:补发|发放|重新发)"
            r"(?:完成|成功|了)?",
            r"(?:优惠券|券).{0,6}(?:已|已经)?"
            rf"(?:发|放|打)(?:到|至|进|入)?(?:{_ACTION_BENEFICIARY})?"
            r".{0,4}(?:账户|账号|卡包)(?:里)?(?:成功|完成|了)?",
            r"(?:优惠券|券).{0,6}(?:已|已经)(?:到账|入账|进账)(?:成功|了)?",
        ),
        frozenset({"issue_coupon", "reissue_coupon"}),
    ),
    (
        "delete_order",
        _extend_action_claim_pattern(
            _action_claim_pattern(r"删除订单|移除订单"),
            r"订单.{0,6}(?:已|已经)(?:删除|移除)(?:完成|成功|了)?",
            rf"订单.{{0,6}}(?:{_ACTION_ALREADY}).{{0,4}}"
            rf"(?:{_ACTION_BENEFICIARY}.{{0,4}})?"
            r"(?:删掉|删了|移掉|移除了)(?:成功|完成|了)?",
        ),
        frozenset({"delete_order"}),
    ),
    (
        "confirm_receipt",
        _extend_action_claim_pattern(
            _action_claim_pattern(r"确认收货"),
            r"(?:已|已经)确认收货(?:完成|成功|了)?",
            rf"(?:{_ACTION_ALREADY}).{{0,4}}{_ACTION_BENEFICIARY}.{{0,4}}"
            r"(?:确认)?收货(?:成功|完成|了)",
        ),
        frozenset({"confirm_order_receipt", "confirm_receipt"}),
    ),
    (
        "extend_receipt",
        _extend_action_claim_pattern(
            _action_claim_pattern(r"延长收货(?:时间|期限)?"),
            r"延长收货(?:时间|期限)?.{0,6}(?:完成|成功|了)",
            rf"收货(?:时间|期限).{{0,6}}(?:{_ACTION_ALREADY}).{{0,4}}"
            rf"(?:{_ACTION_BENEFICIARY}.{{0,4}})?延长(?:成功|完成|了)",
            r"收货(?:时间|期限).{0,6}(?:延长|延)(?:到|至).{0,12}(?:了)",
        ),
        frozenset({"extend_receipt_deadline", "extend_order_receipt"}),
    ),
)
_DELIVERY_EVENT = (
    r"(?:发货|发出|发走|发出去|寄出|寄走|出库|揽收|揽件|交寄|交运|"
    r"出货|交给快递|到货|送达|送到|能到|到手|收到货|收货(?!时间|期限)|派送|"
    r"(?:给|为)(?:您|你)?发(?:货|出)?)"
)
_DELIVERY_TIME_VALUE = (
    r"(?:今天|明天|明早|明晚|后天|今日|明日|当天|当日|次日|今晚|今夜|上午|"
    r"下午|晚上|早上|中午|马上|立即|尽快|很快|稍后|隔天|隔日|翌日|"
    r"本周|下周|本周末|下周末|周末|本月|这个月|下月|下个月|月底|月末|"
    r"年内|年前|节前|假期前|周[一二三四五六日天]|"
    r"(?:春节|元旦|清明|劳动节|端午|七夕|中秋|国庆|双十一|双十二|"
    r"618|大促)(?:前|后|期间)|"
    r"\d{1,2}(?:点(?:半|\d{1,2}分)?|时)(?:前|后|左右|之前|以后)?|"
    r"[一二两三四五六七八九十]+(?:点(?:半|[一二两三四五六七八九十]+分)?|时)"
    r"(?:前|后|左右|之前|以后)?|"
    r"\d{1,4}(?:\.\d+)?(?:\s*[-到至~]\s*\d{1,4}(?:\.\d+)?)?\s*"
    r"(?:个)?(?:分钟|小时|天|日|工作日|周|星期|礼拜)(?:内|后|左右|之内)?|"
    r"\d{1,4}(?:\.\d+)?\s*(?:h|H|hr|HR|hrs|HRS)(?:内|后|左右)?|"
    r"[Tt]\s*\+\s*\d+|"
    r"[一二两三四五六七八九十]+(?:到|至|-)?[一二两三四五六七八九十]*\s*"
    r"(?:个)?(?:分钟|小时|天|日|工作日|周|星期|礼拜)(?:内|后|左右|之内)?|"
    r"20\d{2}[-年/]\d{1,2}(?:[-月/]\d{1,2}日?)?|"
    r"\d{1,2}月\d{1,2}日|\d{1,2}/\d{1,2})"
)
_DELIVERY_TIME_CLAIM = re.compile(
    rf"{_DELIVERY_TIME_VALUE}.{{0,16}}?{_DELIVERY_EVENT}|"
    rf"{_DELIVERY_EVENT}.{{0,16}}?{_DELIVERY_TIME_VALUE}"
)
_DELIVERY_UNCERTAINTY = re.compile(
    rf"(?:无法|不能|暂时无法|尚不能|不敢|不会).{{0,10}}"
    rf"(?:确认|保证|承诺).{{0,16}}{_DELIVERY_EVENT}|"
    rf"(?:能否|是否|可否).{{0,16}}{_DELIVERY_EVENT}|"
    rf"{_DELIVERY_EVENT}.{{0,12}}(?:无法|不能|暂时无法|尚不能).{{0,8}}"
    rf"(?:确认|保证|承诺)|"
    rf"{_DELIVERY_EVENT}.{{0,12}}(?:需要|需|待).{{0,6}}(?:人工)?(?:确认|核对)|"
    rf"(?:不保证|不承诺|无法保证|不能保证|无法承诺|不能承诺|不一定)"
    rf".{{0,16}}{_DELIVERY_EVENT}|"
    rf"(?:能不能|会不会|可不可以|是否|能否|可否).{{0,16}}{_DELIVERY_EVENT}"
)
_DELIVERY_CERTAINTY = re.compile(r"保证|承诺|一定|百分之百|100%|确保")
_DELIVERY_TENTATIVE = re.compile(
    r"预计|预估|大概|一般|应该|可能|或许|约|大约|通常|"
    r"正常(?:情况下)?|暂定|仅供参考|参考|最晚|最迟|最快"
)
_DELIVERY_NEGATED_CERTAINTY = re.compile(
    r"(?:不|未|并不|并未|不能|无法|不敢|不会)(?:能|会)?(?:保证|承诺|确保)|"
    r"(?:不|未|并不|并非|不是)一定|(?:并非|不是)百分之百|(?:并非|不是)100%"
)
_ACTION_CLAIM_NONASSERTIVE = re.compile(
    r"没有|尚未|还没|并未|未曾|不曾|未能|不是|"
    r"不能说|不应说|不要说|请勿说|无法确认|不能确认|"
    r"如果|假如|假设|若|是否|能否|可否|能不能|可不可以|"
    r"有没有|是不是|需要.{0,4}吗|吗$|么$|呢$"
)
_ACTION_CLAUSE_SPLIT = re.compile(
    r"[，。；！？,;!?]|但是|不过|而是|但|却|同时|并且|而且|另外|然后"
)
_REFUND_DESTINATION = (
    r"(?:(?:您|你)的?)?(?:原)?(?:支付)?"
    r"(?:账户|账号|银行卡|原卡|余额|支付方式|支付账户|渠道)"
)
_REFUND_ACCOUNT_RETURN = (
    rf"(?:(?:原路)?返(?:回|还)|(?:退|返)(?:至|到|入).{{0,4}}"
    rf"{_REFUND_DESTINATION})"
)
_PASSIVE_REFUND_APPLICATION_STATUS = re.compile(
    r"(?:退款|退货|换货|售后)申请.{0,8}(?:已|已经)(?:提交|受理)"
)
_AGENT_ACTION_ACTOR = re.compile(
    r"(?:我|我们|客服|这边|本店)|"
    r"(?:(?:为|给|帮|替)(?:您|你))"
)
_CONTEXTUAL_COMPLETION_AFTER_ALREADY = (
    r"(?:处理|办理|操作|提交|安排|完成|办|办好|办妥|搞定|弄好|弄妥|"
    r"改好|换好|加好|改了|换了|加了|退回(?:去)?|退掉|"
    r"撤单|撤了单|撤掉|关单|关掉?|作废|"
    r"补发|重新寄(?:出)?|改过来|加急|(?:原路)?返(?:回|还)|"
    rf"退(?:至|到)(?:原支付账户|原支付方式|原账户|原渠道)|"
    rf"{_REFUND_ACCOUNT_RETURN})"
    r"(?:好|妥|完|完成|成功|了)?"
)
_CONTEXTUAL_COMPLETION_WITHOUT_ALREADY = (
    r"(?:处理(?:好|完|完成|成功|了)|办理(?:好|完|完成|成功|了)|"
    r"操作(?:完成|成功|了)|提交(?:完成|成功|了)|安排(?:好|完成|成功|了)|"
    r"完成(?:了)?|办(?:好|妥|完|了)|弄(?:好|妥|完)(?:了)?|搞定(?:了)?|"
    r"改(?:好|妥|完|了)(?:了)?|换(?:好|妥|完|了)(?:了)?|"
    r"加(?:好|妥|完|了)(?:了)?|"
    r"退回(?:去)?(?:完成|成功|了)|退掉(?:了)?|撤单(?:完成|成功|了)?|"
    r"撤了单|撤掉(?:了)?|作废(?:了)?|改过来(?:了)?|"
    r"重新寄(?:出)?(?:了)?|加急(?:了)?|"
    r"关单(?:完成|成功|了)?|关(?:好|掉|了)|补发(?:完成|成功|了)?|"
    r"(?:原路)?返(?:回|还)(?:完成|成功|了)?|"
    r"退(?:至|到)(?:原支付账户|原支付方式|原账户|原渠道)(?:了)?|"
    rf"{_REFUND_ACCOUNT_RETURN}(?:完成|成功|了)?)"
)
_CONTEXTUAL_ACTION_COMPLETION_CLAIM = re.compile(
    rf"(?:{_ACTION_ALREADY}.{{0,4}}{_ACTION_BENEFICIARY}.{{0,6}}"
    rf"{_CONTEXTUAL_COMPLETION_AFTER_ALREADY}|"
    rf"{_ACTION_ACTOR}.{{0,6}}(?:{_ACTION_ALREADY}.{{0,4}}"
    rf"{_CONTEXTUAL_COMPLETION_AFTER_ALREADY}|"
    rf"{_CONTEXTUAL_COMPLETION_WITHOUT_ALREADY})|"
    rf"{_ACTION_ALREADY}.{{0,4}}{_CONTEXTUAL_COMPLETION_AFTER_ALREADY}|"
    rf"{_CONTEXTUAL_COMPLETION_WITHOUT_ALREADY}|"
    r"^(?:好(?:了|啦)|成功(?:了|啦)|"
    r"(?:已|已经)?退款(?:完成|成功|了)?|退了|"
    r"(?:已|已经)?(?:取消|撤销|关闭|作废)(?:了)?)$)"
)
_CONTEXTUAL_EXPLICIT_ACTION_RESULTS = (
    (
        re.compile(r"^(?:(?:已|已经)?退款(?:完成|成功|了)?|退了)$"),
        frozenset({"refund_order"}),
    ),
    (
        re.compile(r"^(?:已|已经)?(?:取消|撤销|关闭|作废)(?:了)?$"),
        frozenset({"cancel_order", "order_cancel"}),
    ),
)
_ACTION_REQUEST_PREFIX = (
    r"(?:(?:请|麻烦)?(?:帮|给|替)(?:我|我们)|"
    r"(?:立即|马上|现在)(?:(?:帮|给|替)(?:我|我们))?)"
)
_ACTION_INFORMATION_MARKER = re.compile(
    r"(?:查询|查看|查一下|查下|看看|了解|咨询|询问|问一下)"
)
_ACTION_SEQUENCE_PREFIX = re.compile(
    r"^(?:先|再|然后|接着|随后|之后|下一步)\s*"
)
_ACTION_AFTER_INFORMATION_CONNECTOR = re.compile(
    r"^(?:一下|下)?(?:并|同时|然后|再|接着|随后|之后|后)"
    r".{0,6}(?:办理|申请|操作|处理|发起|执行)?"
)
_CONTEXTUAL_ACTION_REQUESTS = (
    (
        re.compile(
            rf"(?:{_ACTION_REQUEST_PREFIX}.{{0,6}}"
            r"(?:办理|申请|操作|处理|发起)?(?:退款|退钱)|"
            r"^(?:请|麻烦)?(?:办理|申请|操作|处理|发起)?"
            r"(?:退款|退钱)(?:一下|吧)?[。！!]?$)"
        ),
        frozenset({"refund_order"}),
    ),
    (
        re.compile(
            rf"(?:{_ACTION_REQUEST_PREFIX}.{{0,6}}"
            r"(?:办理|申请|操作|处理|发起)?退货|"
            r"^(?:请|麻烦)?(?:办理|申请|操作|处理|发起)?"
            r"退货(?:一下|吧)?[。！!]?$)"
        ),
        frozenset({"return_order"}),
    ),
    (
        re.compile(
            rf"(?:{_ACTION_REQUEST_PREFIX}.{{0,6}}"
            r"(?:办理|申请|操作|处理|发起)?换货|"
            r"^(?:请|麻烦)?(?:办理|申请|操作|处理|发起)?"
            r"换货(?:一下|吧)?[。！!]?$)"
        ),
        frozenset({"exchange_order"}),
    ),
    (
        re.compile(
            rf"(?:{_ACTION_REQUEST_PREFIX}.{{0,6}}"
            r"(?:办理|申请|操作|处理|发起)?(?:赔付|赔偿|补偿)|"
            r"^(?:请|麻烦)?(?:办理|申请|操作|处理|发起)?"
            r"(?:赔付|赔偿|补偿)(?:一下|吧)?[。！!]?$)"
        ),
        frozenset({"compensate_order"}),
    ),
    (
        re.compile(
            rf"{_ACTION_REQUEST_PREFIX}.{{0,8}}"
            r"(?:取消|撤销|关闭|关掉|作废)(?:这个)?订单|"
            r"(?:把|将).{0,6}订单.{0,4}(?:取消|撤销|关闭|关掉|作废)"
            r"|^(?:请|麻烦)?(?:取消|撤销|关闭|关掉|作废)(?:这个)?订单"
            r"(?:一下|吧)?[。！!]?$"
        ),
        frozenset({"cancel_order", "order_cancel"}),
    ),
    (
        re.compile(
            rf"{_ACTION_REQUEST_PREFIX}.{{0,6}}"
            r"(?:改|修改|更改|更新|变更|调整|换)(?:收货)?地址|"
            r"(?:把|将).{0,6}(?:收货)?地址.{0,6}(?:改|修改|更改|更新)"
            r"|^(?:请|麻烦)?(?:改|修改|更改|更新|变更|调整|换)"
            r"(?:收货)?地址[。！!]?$"
        ),
        frozenset({"update_order_address", "change_order_address"}),
    ),
    (
        re.compile(
            rf"(?:{_ACTION_REQUEST_PREFIX}.{{0,6}}"
            r"(?:补发|重新发|重发|重新寄|再寄)(?:商品|货物|这一件|一件)?|"
            r"^(?:请|麻烦)?(?:补发|重新发|重发|重新寄|再寄)"
            r"(?:商品|货物|这一件|一件)?(?:一下|吧)?[。！!]?$)"
        ),
        frozenset({"reship_order", "create_replacement_shipment"}),
    ),
    (
        re.compile(
            rf"(?:{_ACTION_REQUEST_PREFIX}.{{0,6}}"
            r"(?:改价|修改价格|更改价格|调整价格|变更价格)|"
            r"^(?:请|麻烦)?(?:改价|修改价格|更改价格|调整价格|变更价格)"
            r"(?:一下|吧)?[。！!]?$)"
        ),
        frozenset({"update_product_price", "change_price", "update_price"}),
    ),
    (
        re.compile(
            rf"(?:{_ACTION_REQUEST_PREFIX}.{{0,6}}"
            r"(?:改|修改|更改|更新|变更|调整|换)(?:收货人|手机号|联系电话|联系方式)|"
            r"^(?:请|麻烦)?(?:改|修改|更改|更新|变更|调整|换)"
            r"(?:收货人|手机号|联系电话|联系方式)"
            r"[。！!]?$)"
        ),
        frozenset(
            {
                "update_order_contact",
                "update_order_recipient",
                "update_order_phone",
            }
        ),
    ),
    (
        re.compile(
            rf"(?:{_ACTION_REQUEST_PREFIX}.{{0,6}}"
            r"(?:改|修改|更改|更新|变更|调整)发票抬头|"
            r"^(?:请|麻烦)?(?:改|修改|更改|更新|变更|调整)"
            r"发票抬头[。！!]?$)"
        ),
        frozenset({"update_invoice_title", "update_order_invoice_title"}),
    ),
    (
        re.compile(
            rf"(?:{_ACTION_REQUEST_PREFIX}.{{0,6}}"
            r"(?:拦截(?:快递|包裹)|召回包裹|追回包裹)|"
            r"^(?:请|麻烦)?(?:拦截(?:快递|包裹)|召回包裹|追回包裹)"
            r"(?:一下|吧)?[。！!]?$)"
        ),
        frozenset(
            {"intercept_shipment", "intercept_package", "recall_shipment"}
        ),
    ),
    (
        re.compile(
            rf"(?:{_ACTION_REQUEST_PREFIX}.{{0,6}}"
            r"(?:催发货|催促发货|加急发货|催促出库)|"
            r"^(?:请|麻烦)?(?:催发货|催促发货|加急发货|催促出库)"
            r"(?:一下|吧)?[。！!]?$)"
        ),
        frozenset({"expedite_order", "urge_shipment"}),
    ),
    (
        re.compile(
            rf"(?:{_ACTION_REQUEST_PREFIX}.{{0,8}}"
            r"(?:添加|增加|加|修改|更新|变更)(?:订单)?(?:备注|留言)|"
            r"^(?:请|麻烦)?(?:给)?(?:订单)?"
            r"(?:添加|增加|加|修改|更新|变更)(?:订单)?(?:备注|留言)"
            r"(?:一下|吧)?[。！!]?$)"
        ),
        frozenset({"add_order_note", "update_order_note"}),
    ),
    (
        re.compile(
            rf"(?:{_ACTION_REQUEST_PREFIX}.{{0,8}}"
            r"(?:补发|发放|重新发)(?:优惠券|券)|"
            r"^(?:请|麻烦)?(?:补发|发放|重新发)(?:优惠券|券)"
            r"(?:一下|吧)?[。！!]?$)"
        ),
        frozenset({"issue_coupon", "reissue_coupon"}),
    ),
    (
        re.compile(
            rf"(?:{_ACTION_REQUEST_PREFIX}.{{0,8}}(?:删除|移除)(?:这个)?订单|"
            r"^(?:请|麻烦)?(?:删除|移除)(?:这个)?订单(?:一下|吧)?[。！!]?$)"
        ),
        frozenset({"delete_order"}),
    ),
    (
        re.compile(
            rf"(?:{_ACTION_REQUEST_PREFIX}.{{0,8}}确认收货|"
            r"^(?:请|麻烦)?确认收货(?:一下|吧)?[。！!]?$)"
        ),
        frozenset({"confirm_order_receipt", "confirm_receipt"}),
    ),
    (
        re.compile(
            rf"(?:{_ACTION_REQUEST_PREFIX}.{{0,8}}延长收货(?:时间|期限)?|"
            r"^(?:请|麻烦)?延长收货(?:时间|期限)?(?:一下|吧)?[。！!]?$)"
        ),
        frozenset({"extend_receipt_deadline", "extend_order_receipt"}),
    ),
)
_DELIVERY_QUESTION = re.compile(r"(?:吗|么|呢)[？?]?$|[？?]$")
_DELIVERY_REQUEST_CONTEXT = re.compile(
    r"(?:发货|发出|寄出|出货|配送|送达|到货|什么时候发|多久发|何时发)"
)
_DELIVERY_ELLIPTICAL_RESPONSE = re.compile(
    rf"{_DELIVERY_TIME_VALUE}.{{0,8}}"
    r"(?:预计|大概|一般|应该|会|能|可以|可)?(?:会|能|可以|可)?"
    r"(?:(?:发|寄)(?:出|给(?:您|你))?|"
    r"(?:(?:给|为)(?:您|你))?安排(?:发货|出库)?|出库)(?:了)?"
)
_DELIVERY_BARE_TIME_RESPONSE = re.compile(
    rf"^(?:(?:预计|大概|一般|应该|可能|约|大约|最晚|最迟|最快|通常|正常)\s*)?"
    r"(?:(?:我看|看起来|可以确定|肯定|确定|就是)(?:是|就是)?\s*)?"
    rf"(?:在\s*)?{_DELIVERY_TIME_VALUE}"
    r"(?:\s*(?:左右|前|后|内|之内))?"
    r"(?:\s*(?:(?:肯定|当然)?(?:可以|能|会|行)(?:的|了)?|"
    r"没问题|就(?:可以|能|行)(?:了)?))?(?:吧|呢)?$"
)
_DELIVERY_CONTEXTUAL_ARRIVAL = re.compile(
    rf"{_DELIVERY_TIME_VALUE}.{{0,8}}(?:达|到|收)(?:货|手)?"
)
_DELIVERY_CONTEXTUAL_ARRIVAL_UNCERTAINTY = re.compile(
    r"(?:无法|不能|不敢|不会|不保证|不承诺|不一定).{0,12}"
    r"(?:达|到|收)(?:货|手)?|"
    r"(?:达|到|收)(?:货|手)?.{0,12}"
    r"(?:无法|不能|不敢|不会|不保证|不承诺|不一定)"
)
_NEVER_ALLOWED_OUTPUT_PATTERNS = (
    r"(请提供|发送).{0,6}(密码|验证码|完整身份证|银行卡密码)",
    r"(加我微信|转到私人账户|站外支付)",
)
_FORBIDDEN_OUTPUT_NEGATION = re.compile(
    r"(?:请勿|不要|切勿|不得|不能|不可|别)[^，。；！？,;!?]{0,12}$"
)
FORBIDDEN_OUTPUT_PATTERNS = (
    *(pattern.pattern for _, pattern, _ in _BUSINESS_ACTION_OUTPUT_CLAIMS),
    _DELIVERY_TIME_CLAIM.pattern,
    *_NEVER_ALLOWED_OUTPUT_PATTERNS,
)


def _contains_never_allowed_output(answer: str) -> bool:
    for pattern in _NEVER_ALLOWED_OUTPUT_PATTERNS:
        for match in re.finditer(pattern, answer):
            prefix = answer[max(0, match.start() - 18) : match.start()]
            if not _FORBIDDEN_OUTPUT_NEGATION.search(prefix):
                return True
    return False

# Internal identifiers a shopper cannot be expected to know. The agent must resolve
# them from the wording the customer already used instead of asking for them.
INTERNAL_IDENTIFIER_FIELDS = {
    "sku",
    "sku_id",
    "skuid",
    "sku_code",
    "item_id",
    "itemid",
    "num_iid",
    "product_id",
    "productid",
    "product_code",
    "spu",
    "spu_id",
    "spuid",
    "goods_id",
    "catalog_id",
    "catalog_item_id",
}

INTERNAL_IDENTIFIER_LABEL = "商品名称或商品链接"

INTERNAL_IDENTIFIER_REQUEST_PATTERNS = (
    r"sku",
    r"(item|product|spu|goods)[\s_-]*id",
    r"(商品|宝贝|货品)\s*(id|编号|编码|货号|代码)",
)

ALLOWED_CONTEXT_FIELDS = {
    "authorized",
    "platform",
    "store_id",
    "shop_id",
    "product_name",
    "sku_id",
    "sku",
    "order_id",
    "order_status",
    "logistics_status",
    "carrier",
    "tracking_last_event",
    "shop_policy",
}


@dataclass(frozen=True, slots=True)
class PrecheckDecision:
    route: str
    reason: str


def sanitize_context(context: dict[str, Any]) -> dict[str, Any]:
    sanitized: dict[str, Any] = {}
    for key, value in context.items():
        if key not in ALLOWED_CONTEXT_FIELDS:
            continue
        if isinstance(value, bool):
            sanitized[key] = value
        elif isinstance(value, (str, int, float)):
            normalized = normalize_text(str(value))[:500]
            sanitized[key] = redact_sensitive(normalized)[0]
    return sanitized


def precheck_request(message: str, context: dict[str, Any]) -> PrecheckDecision:
    """Enforce trust boundaries without deciding normal business intent."""

    for pattern in PROMPT_INJECTION_PATTERNS:
        if re.search(pattern, message, re.IGNORECASE):
            return PrecheckDecision("refuse", "prompt_injection_detected")
    if PROMPT_DISCLOSURE_ACTION_PATTERN.search(
        message
    ) and PROMPT_DISCLOSURE_TARGET_PATTERN.search(message):
        return PrecheckDecision("refuse", "prompt_injection_detected")
    for pattern in UNAUTHORIZED_DATA_PATTERNS:
        if re.search(pattern, message, re.IGNORECASE):
            return PrecheckDecision("refuse", "unauthorized_data_request")
    return PrecheckDecision("deliberate", "llm_deliberation_allowed")


def is_business_action_request(message: str) -> bool:
    """Detect actions that require verified execution or a human handoff."""

    return any(re.search(pattern, message) for pattern in HIGH_RISK_ACTION_PATTERNS)


def asks_for_internal_identifier(text: str) -> bool:
    """Detect a reply that demands SKU/item ids a shopper does not have."""

    return any(
        re.search(pattern, text, re.IGNORECASE)
        for pattern in INTERNAL_IDENTIFIER_REQUEST_PATTERNS
    )


def customer_facing_missing_fields(fields: list[str]) -> list[str]:
    """Replace internal identifier field names with what a shopper can provide."""

    described: list[str] = []
    for field in fields:
        label = field.strip()
        if label.lower().replace("-", "_") in INTERNAL_IDENTIFIER_FIELDS:
            label = INTERNAL_IDENTIFIER_LABEL
        if label and label not in described:
            described.append(label)
    return described


def review_output(
    answer: str,
    evidence: str,
    *,
    verified_business_action: str | None = None,
    approved_commitment: bool = False,
    verified_delivery_commitment: bool = False,
    question: str | None = None,
) -> tuple[bool, str]:
    if not answer.strip():
        return False, "empty_model_output"
    tool_name = str(verified_business_action or "").lower()
    if not business_action_completion_claim_is_authorized(
        answer,
        tool_name,
        question=question,
    ):
        return False, "forbidden_commitment_in_output"
    if (
        delivery_time_claim_requires_support(answer, question=question)
        and not approved_commitment
        and not verified_delivery_commitment
    ):
        return False, "forbidden_commitment_in_output"
    if _contains_never_allowed_output(answer):
        return False, "forbidden_commitment_in_output"
    # Treat 499 and 499.00 as equal; keep percentages distinct.
    unsupported_numbers = _normalized_numbers(answer) - _normalized_numbers(evidence)
    if unsupported_numbers:
        return False, "numeric_claim_without_evidence"
    return True, "output_policy_passed"


def has_business_action_completion_claim(
    answer: str,
    *,
    question: str | None = None,
) -> bool:
    has_contextual_request, _ = _contextual_action_allowed_tools(question or "")
    return any(
        _has_asserted_action_claim(answer, pattern, action_kind=action_kind)
        for action_kind, pattern, _ in _BUSINESS_ACTION_OUTPUT_CLAIMS
    ) or bool(
        has_contextual_request
        and _has_asserted_action_claim(answer, _CONTEXTUAL_ACTION_COMPLETION_CLAIM)
    )


def business_action_completion_claim_is_authorized(
    answer: str,
    verified_business_action: str | None,
    *,
    question: str | None = None,
) -> bool:
    tool_name = str(verified_business_action or "").lower()
    has_contextual_request, contextual_tools = _contextual_action_allowed_tools(
        question or ""
    )
    explicit_claims = [
        allowed_tool_names
        for action_kind, pattern, allowed_tool_names in _BUSINESS_ACTION_OUTPUT_CLAIMS
        if _has_asserted_action_claim(answer, pattern, action_kind=action_kind)
    ]
    if has_contextual_request:
        explicit_claims.extend(
            allowed_tool_names
            for pattern, allowed_tool_names in _CONTEXTUAL_EXPLICIT_ACTION_RESULTS
            if _has_asserted_action_claim(
                answer,
                pattern,
                action_kind="contextual",
            )
        )
    if explicit_claims:
        return all(tool_name in allowed_tool_names for allowed_tool_names in explicit_claims)
    if has_contextual_request and _has_asserted_action_claim(
        answer,
        _CONTEXTUAL_ACTION_COMPLETION_CLAIM,
        action_kind="contextual",
    ):
        return tool_name in contextual_tools
    return True


def _contextual_action_allowed_tools(
    question: str,
) -> tuple[bool, frozenset[str]]:
    matching: list[frozenset[str]] = []
    for raw_clause in _ACTION_CLAUSE_SPLIT.split(question):
        clause = _ACTION_SEQUENCE_PREFIX.sub("", raw_clause.strip())
        information_markers = list(_ACTION_INFORMATION_MARKER.finditer(clause))
        for pattern, allowed_tool_names in _CONTEXTUAL_ACTION_REQUESTS:
            for request_match in pattern.finditer(clause):
                overlapping_markers = [
                    marker
                    for marker in information_markers
                    if (
                    marker.start() < request_match.end()
                    and marker.end() > request_match.start()
                    )
                ]
                if overlapping_markers and not any(
                    _ACTION_AFTER_INFORMATION_CONNECTOR.search(
                        clause[marker.end() : request_match.end()]
                    )
                    for marker in overlapping_markers
                ):
                    continue
                matching.append(allowed_tool_names)
    if not matching:
        return False, frozenset()
    return True, frozenset.intersection(*matching)


def _has_asserted_action_claim(
    answer: str,
    pattern: re.Pattern[str],
    *,
    action_kind: str | None = None,
) -> bool:
    for clause in _ACTION_CLAUSE_SPLIT.split(answer):
        for match in pattern.finditer(clause):
            if (
                action_kind in {"refund", "contextual"}
                and _PASSIVE_REFUND_APPLICATION_STATUS.search(clause)
                and not _AGENT_ACTION_ACTOR.search(clause)
            ):
                continue
            local_start = max(0, match.start() - 12)
            local_end = min(len(clause), match.end() + 4)
            if not _ACTION_CLAIM_NONASSERTIVE.search(
                clause[local_start:local_end]
            ):
                return True
    return False


def has_delivery_time_claim(answer: str) -> bool:
    return _DELIVERY_TIME_CLAIM.search(answer) is not None


def delivery_time_claim_segments(answer: str) -> list[str]:
    return [match.group(0) for match in _DELIVERY_TIME_CLAIM.finditer(answer)]


def delivery_time_claim_is_uncertain(answer: str) -> bool:
    clauses = [
        clause
        for clause in _ACTION_CLAUSE_SPLIT.split(answer)
        if _DELIVERY_TIME_CLAIM.search(clause)
    ]
    return bool(clauses) and all(
        _DELIVERY_UNCERTAINTY.search(clause) is not None
        and not delivery_time_claim_uses_certainty(clause)
        for clause in clauses
    )


def delivery_time_claim_uses_certainty(answer: str) -> bool:
    for match in _DELIVERY_CERTAINTY.finditer(answer):
        local_start = max(0, match.start() - 4)
        if not _DELIVERY_NEGATED_CERTAINTY.search(answer[local_start : match.end()]):
            return True
    return False


def delivery_time_claim_confidence(answer: str) -> int:
    """Rank how strongly a delivery-time clause asserts its timing."""

    if delivery_time_claim_is_uncertain(answer):
        return 0
    if delivery_time_claim_uses_certainty(answer):
        return 3
    if _DELIVERY_TENTATIVE.search(answer):
        return 1
    return 2


def delivery_time_claim_requires_support(
    answer: str,
    *,
    question: str | None = None,
) -> bool:
    for clause in _ACTION_CLAUSE_SPLIT.split(answer):
        has_delivery_question_context = bool(
            question and _DELIVERY_REQUEST_CONTEXT.search(question)
        )
        contextual_ellipsis = bool(
            has_delivery_question_context
            and _DELIVERY_ELLIPTICAL_RESPONSE.search(clause)
        )
        contextual_arrival = bool(
            has_delivery_question_context
            and _DELIVERY_CONTEXTUAL_ARRIVAL.search(clause)
        )
        contextual_bare_time = bool(
            has_delivery_question_context
            and _DELIVERY_BARE_TIME_RESPONSE.search(clause.strip())
        )
        if (
            not _DELIVERY_TIME_CLAIM.search(clause)
            and not contextual_ellipsis
            and not contextual_arrival
            and not contextual_bare_time
        ):
            continue
        if delivery_time_claim_uses_certainty(clause):
            return True
        if _DELIVERY_QUESTION.search(clause):
            continue
        has_uncertainty = _DELIVERY_UNCERTAINTY.search(clause) is not None
        if contextual_arrival:
            has_uncertainty = has_uncertainty or (
                _DELIVERY_CONTEXTUAL_ARRIVAL_UNCERTAINTY.search(clause) is not None
            )
        if not has_uncertainty:
            return True
    return False


def _normalized_numbers(text: str) -> set[str]:
    values: set[str] = set()
    for raw in re.findall(r"\d+(?:\.\d+)?%?", text):
        percent = raw.endswith("%")
        number_text = raw[:-1] if percent else raw
        try:
            number = Decimal(number_text)
        except InvalidOperation:
            continue
        values.add(("%" if percent else "") + format(number.normalize(), "f"))
    return values
