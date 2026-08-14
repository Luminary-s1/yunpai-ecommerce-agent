// 02_load_rels.cypher — 加载关系（按通用 id 匹配两端 + MERGE）
LOAD CSV WITH HEADERS FROM 'file:///kg/rels_belongs_to.csv' AS row
MATCH (a {id: row.source}), (b {id: row.target})
MERGE (a)-[r:BELONGS_TO]->(b)
SET r.updated_at = row.updated_at, r.confidence = row.confidence, r.generated_by = row.generated_by;
LOAD CSV WITH HEADERS FROM 'file:///kg/rels_has_attr.csv' AS row
MATCH (a {id: row.source}), (b {id: row.target})
MERGE (a)-[r:HAS_ATTR]->(b)
SET r.updated_at = row.updated_at, r.confidence = row.confidence, r.generated_by = row.generated_by;
LOAD CSV WITH HEADERS FROM 'file:///kg/rels_applies_to.csv' AS row
MATCH (a {id: row.source}), (b {id: row.target})
MERGE (a)-[r:APPLIES_TO]->(b)
SET r.updated_at = row.updated_at, r.confidence = row.confidence, r.generated_by = row.generated_by;
LOAD CSV WITH HEADERS FROM 'file:///kg/rels_refers_to.csv' AS row
MATCH (a {id: row.source}), (b {id: row.target})
MERGE (a)-[r:REFERS_TO]->(b)
SET r.updated_at = row.updated_at, r.confidence = row.confidence, r.generated_by = row.generated_by, r.target_type = row.target_type;
LOAD CSV WITH HEADERS FROM 'file:///kg/rels_related_to.csv' AS row
MATCH (a {id: row.source}), (b {id: row.target})
MERGE (a)-[r:RELATED_TO]->(b)
SET r.updated_at = row.updated_at, r.confidence = row.confidence, r.generated_by = row.generated_by;
