# 数据字典（data_dictionary）

## 1. 知识分类（对齐任务书四大类）

| 知识类别 | 文件 | 条数 |
|---|---|---|
| 商品信息 | product.json / sku | 12 SKU（8 SPU） |
| 售后政策 | policy.json | 9 |
| 客服话术 | script.json | 52 |
| 行业规则 | rule.json | 9 |
| 常见问答 FAQ | faq.json | 60 |

## 2. 六类实体 + 五类关系

**实体**：Category（品类）/ Product+SKU（商品）/ Attribute（属性）/ Policy（售后政策）/ Script（客服话术）/ FAQ

**关系**：BELONGS_TO（属于）/ HAS_ATTR（具有）/ APPLIES_TO（适用）/ REFERS_TO（引用）/ RELATED_TO（关联）

**机器可读契约**：`03_dictionary/dictionary_schema.json`（下游 Wiki/检索 API/Prompt 对齐用）

## 3. 枚举字典

- **品类 category_code**：air_circulation_fan, air_fryer, apparel, cordless_vacuum, digital, digital_audio, digital_power, electric_kettle, home_appliance, humidifier
- **品类中文名**：加湿器, 小家电, 循环风扇, 数码, 数码电源, 数码音频, 无线吸尘器, 服饰, 电热水壶, 空气炸锅
- **政策类型 policy_type**：logistics, price_protection, return, warranty
- **风险等级 risk_level**：high, low, medium
- **知识层级 layer**：product, store

## 4. 字段契约（各 JSON 数组结构）

### category.json
`category_code | category_name | parent_category | level`
- `category_code`：唯一键；`level`：1=顶层，2=二级

### product.json
`item_id | sku_id | title | model | status | sale_price | warranty_months | category | category_name | spu_attributes | sku_attributes | selling_points | source`
- `item_id`：SPU 唯一键；`sku_id`：SKU 唯一键
- `spu_attributes`：SPU 级属性（品牌/型号/保修）；`sku_attributes`：SKU 级属性（颜色/容量/尺码等）
- `selling_points`：卖点列表（任务书要求）

### attribute.json
`spec_key | attr_key | attr_value | level | owner_id`
- `spec_key = {item_id}|{attr_key}`（SPU 级）或 `{sku_id}|{attr_key}`（SKU 级）

### policy.json
`policy_code | policy_type | policy_name | content | scope | scope_key | risk_level | effective_from | effective_to | source | source_url`
- `policy_code = {PREFIX}-{hash8}`（如 RETURN-a1b2c3d4）

### script.json
`script_id | category | intent | keywords | canonical_answer | questions | risk_level | layer | source`

### faq.json
`faq_id | category | intent | question | answer | keywords | risk_level | layer | ref_script_id | sku_id | source`
- `ref_script_id`：引用话术 ID，answer 可派生（§2.1⑥）

## 5. 数据规范

- 价格 decimal(10,2)+CNY；日期 ISO8601；文本全半角统一；UTF-8 无 BOM
- 安全：无明文手机/地址（种子已用 buyer_ref_hash 脱敏）
- 增量导入：CSV 带 updated_at，MERGE 幂等（§8.1）

## 6. 与后续任务衔接

- **Wiki 分类**：复用 `layer` + `category` 枚举做目录树
- **知识图谱实体**：六类实体 + 五类关系已建（`02_clean/*.json` + `04_import/*.csv`）
- **检索 API**：字段名直接取自 dictionary_schema.json，保持契约一致
- **Prompt 注入**：防幻觉指令注入字段直接取本契约
