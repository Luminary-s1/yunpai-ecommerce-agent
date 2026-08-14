// 00_setup.cypher — 唯一约束与索引（幂等，可重跑）
CREATE CONSTRAINT unique_Category IF NOT EXISTS FOR (n:Category) REQUIRE n.category_code IS UNIQUE;
CREATE CONSTRAINT unique_Product IF NOT EXISTS FOR (n:Product) REQUIRE n.item_id IS UNIQUE;
CREATE CONSTRAINT unique_SKU IF NOT EXISTS FOR (n:SKU) REQUIRE n.sku_id IS UNIQUE;
CREATE CONSTRAINT unique_Attribute IF NOT EXISTS FOR (n:Attribute) REQUIRE n.spec_key IS UNIQUE;
CREATE CONSTRAINT unique_Policy IF NOT EXISTS FOR (n:Policy) REQUIRE n.policy_code IS UNIQUE;
CREATE CONSTRAINT unique_Script IF NOT EXISTS FOR (n:Script) REQUIRE n.script_id IS UNIQUE;
CREATE CONSTRAINT unique_FAQ IF NOT EXISTS FOR (n:FAQ) REQUIRE n.faq_id IS UNIQUE;
CREATE CONSTRAINT unique_Rule IF NOT EXISTS FOR (n:Rule) REQUIRE n.rule_code IS UNIQUE;
CREATE INDEX idx_attr_key IF NOT EXISTS FOR (a:Attribute) ON (a.attr_key);
CREATE INDEX idx_script_intent IF NOT EXISTS FOR (s:Script) ON (s.intent);
CREATE INDEX idx_faq_intent IF NOT EXISTS FOR (f:FAQ) ON (f.intent);
