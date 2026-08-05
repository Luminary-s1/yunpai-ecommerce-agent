// 01_load_nodes.cypher — 加载节点（MERGE 幂等，SET 通用 id）
LOAD CSV WITH HEADERS FROM 'file:///kg/nodes_category.csv' AS row
MERGE (n:Category {category_code: row.category_code})
SET n.id = row.category_code,
  n.updated_at = row.updated_at,
  n.category_name = row.category_name,
  n.parent_category = row.parent_category,
  n.level = row.level;
LOAD CSV WITH HEADERS FROM 'file:///kg/nodes_product.csv' AS row
MERGE (n:Product {item_id: row.item_id})
SET n.id = row.item_id,
  n.updated_at = row.updated_at,
  n.title = row.title,
  n.model = row.model,
  n.status = row.status,
  n.sale_price = row.sale_price,
  n.warranty_months = row.warranty_months,
  n.category = row.category,
  n.category_name = row.category_name,
  n.source = row.source;
LOAD CSV WITH HEADERS FROM 'file:///kg/nodes_sku.csv' AS row
MERGE (n:SKU {sku_id: row.sku_id})
SET n.id = row.sku_id,
  n.updated_at = row.updated_at,
  n.item_id = row.item_id,
  n.title = row.title,
  n.color = row.color,
  n.status = row.status,
  n.sale_price = row.sale_price,
  n.category = row.category,
  n.source = row.source;
LOAD CSV WITH HEADERS FROM 'file:///kg/nodes_attribute.csv' AS row
MERGE (n:Attribute {spec_key: row.spec_key})
SET n.id = row.spec_key,
  n.updated_at = row.updated_at,
  n.attr_key = row.attr_key,
  n.attr_value = row.attr_value,
  n.level = row.level,
  n.owner_id = row.owner_id;
LOAD CSV WITH HEADERS FROM 'file:///kg/nodes_policy.csv' AS row
MERGE (n:Policy {policy_code: row.policy_code})
SET n.id = row.policy_code,
  n.updated_at = row.updated_at,
  n.policy_type = row.policy_type,
  n.policy_name = row.policy_name,
  n.content = row.content,
  n.scope = row.scope,
  n.scope_key = row.scope_key,
  n.risk_level = row.risk_level,
  n.source = row.source;
LOAD CSV WITH HEADERS FROM 'file:///kg/nodes_script.csv' AS row
MERGE (n:Script {script_id: row.script_id})
SET n.id = row.script_id,
  n.updated_at = row.updated_at,
  n.category = row.category,
  n.intent = row.intent,
  n.keywords = row.keywords,
  n.canonical_answer = row.canonical_answer,
  n.risk_level = row.risk_level,
  n.layer = row.layer,
  n.source = row.source;
LOAD CSV WITH HEADERS FROM 'file:///kg/nodes_faq.csv' AS row
MERGE (n:FAQ {faq_id: row.faq_id})
SET n.id = row.faq_id,
  n.updated_at = row.updated_at,
  n.category = row.category,
  n.intent = row.intent,
  n.question = row.question,
  n.answer = row.answer,
  n.risk_level = row.risk_level,
  n.layer = row.layer,
  n.ref_script_id = row.ref_script_id,
  n.sku_id = row.sku_id,
  n.source = row.source;
LOAD CSV WITH HEADERS FROM 'file:///kg/nodes_rule.csv' AS row
MERGE (n:Rule {rule_code: row.rule_code})
SET n.id = row.rule_code,
  n.updated_at = row.updated_at,
  n.rule_title = row.rule_title,
  n.authority = row.authority,
  n.theme = row.theme,
  n.content_summary = row.content_summary,
  n.source = row.source,
  n.source_url = row.source_url,
  n.captured_at = row.captured_at;
