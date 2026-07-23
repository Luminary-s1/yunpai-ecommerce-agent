from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


ModuleStatus = Literal["available", "interface", "planned"]


class BusinessModule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    module_id: str
    display_name: str
    status: ModuleStatus
    responsibilities: list[str]
    boundaries: list[str]
    agent_tools: list[str]


def business_module_catalog() -> list[BusinessModule]:
    return [
        BusinessModule(
            module_id="catalog",
            display_name="商品管理",
            status="available",
            responsibilities=["SPU/SKU 主数据", "渠道商品映射", "商品事实与版本"],
            boundaries=["不直接发布渠道商品", "外部事实必须经连接器进入"],
            agent_tools=["get_product_facts"],
        ),
        BusinessModule(
            module_id="orders",
            display_name="订单与售后",
            status="available",
            responsibilities=["订单统一视图", "售后与退款事实", "物流状态"],
            boundaries=["不替代 ERP/OMS", "V1 不自动退款或赔付"],
            agent_tools=["get_order_facts"],
        ),
        BusinessModule(
            module_id="inventory",
            display_name="仓储管理",
            status="available",
            responsibilities=["库存余额", "缺货与滞销识别", "补货建议"],
            boundaries=["不自建 WMS", "采购和调拨只生成建议"],
            agent_tools=["get_inventory_risk"],
        ),
        BusinessModule(
            module_id="competitive_intelligence",
            display_name="竞品分析",
            status="available",
            responsibilities=[
                "可解释同款匹配",
                "版本化人工裁决",
                "价格与内容口碑证据",
                "持久告警处置",
                "来源和估算标识",
            ],
            boundaries=[
                "不抓取未授权数据",
                "未批准匹配不进入 Agent 建议",
                "不保存评论者或原始评论",
                "第三方估算不得冒充店铺真实事实",
            ],
            agent_tools=[
                "get_competitor_price_analysis",
                "get_competitive_intelligence",
            ],
        ),
        BusinessModule(
            module_id="marketing",
            display_name="营销与投放",
            status="available",
            responsibilities=["广告指标", "投放诊断", "内容和预算建议"],
            boundaries=["V1 不做实时竞价", "预算修改必须审批"],
            agent_tools=["get_marketing_diagnosis"],
        ),
        BusinessModule(
            module_id="finance",
            display_name="利润与对账",
            status="available",
            responsibilities=["费用归集", "经营利润", "对账异常"],
            boundaries=["不替代财务总账", "模型不得修改数值"],
            agent_tools=["get_profit_reconciliation"],
        ),
        BusinessModule(
            module_id="metrics",
            display_name="经营指标",
            status="available",
            responsibilities=["指标定义", "数据水位", "异常检测和证据"],
            boundaries=["模型不得直接拼接 SQL", "数据质量失败时不输出经营结论"],
            agent_tools=["get_business_metric"],
        ),
        BusinessModule(
            module_id="customer_service",
            display_name="客服与售后协同",
            status="available",
            responsibilities=["有界 ReAct", "知识问答", "渠道运行与人工接管"],
            boundaries=["无真实权限时使用虚拟接口", "不确定结果不得宣称完成"],
            agent_tools=[],
        ),
        BusinessModule(
            module_id="customer_service_evaluation",
            display_name="客服 Agent 评测",
            status="available",
            responsibilities=[
                "版本化标注集",
                "隔离多轮 Agent 运行",
                "场景指标与回归比较",
                "发布门禁联动",
            ],
            boundaries=[
                "客户标注必须去标识化",
                "冻结数据集不可原位修改",
                "评测运行不写入生产会话",
                "门禁结果不替代双人发布审批",
            ],
            agent_tools=[],
        ),
    ]
