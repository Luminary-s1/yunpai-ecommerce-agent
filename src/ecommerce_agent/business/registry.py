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
            agent_tools=["search_products", "get_product_facts"],
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
            module_id="ops_assistant",
            display_name="运营辅助与文案",
            status="available",
            responsibilities=[
                "CSV/JSON 与表单运营数据解析",
                "多风格营销文案小批量生成",
                "趋势分析与优化建议报告",
            ],
            boundaries=[
                "统计数值由代码计算，模型不得修改",
                "候选文案必须人工审核后才能发布",
                "只输出解读与建议，不执行预算、价格或库存变更",
            ],
            agent_tools=[],
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
            module_id="traffic_lab",
            display_name="商品流量实验室",
            status="available",
            responsibilities=["版本与流量观测", "实验分析证据", "假设与反证"],
            boundaries=[
                "Agent 只读已固化分析，不重算统计",
                "不宣称平台权重，不自动发布、换图、改标题或投放",
            ],
            agent_tools=["get_listing_traffic_insights"],
        ),
        BusinessModule(
            module_id="forecasting",
            display_name="需求预测与库存计划",
            status="available",
            responsibilities=["日需求事实", "区间预测与回测", "确定性库存风险与建议"],
            boundaries=[
                "Agent 只读已固化 run 与 plan，不重算预测或补货量",
                "只输出 advisory 建议，不创建采购单、不付款、不调整库存",
            ],
            agent_tools=["get_demand_forecast", "get_inventory_plan"],
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
