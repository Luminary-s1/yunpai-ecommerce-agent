# Neo4j 部署与图谱导入说明

本目录包含知识图谱的导入文件（`nodes_*.csv` / `rels_*.csv` + 3 个 Cypher），
由 `05_scripts/06_export.py` 从 `02_clean/` 生成。

## 一、部署方式（二选一）

### 方式 A：docker-compose 一键启动（推荐）

```bash
cd <项目根目录>
docker compose up -d
# 等待容器就绪（首次拉镜像约 1-3 分钟）
docker compose ps
```

- HTTP: `http://localhost:7474`（浏览器打开能看到 Neo4j 浏览器）
- Bolt: `bolt://localhost:7687`
- 账号: `neo4j` / `${NEO4J_PASSWORD:-change-me}`（本地开发默认；生产请在 docker-compose.yml 改 `NEO4J_AUTH`）

### 方式 B：本机安装 Neo4j Community 手动部署

1. 下载 Neo4j Community 5.x：https://neo4j.com/download-center/
2. 解压后设置初始密码：
   ```bash
   <neo4j-home>/bin/neo4j-admin dbms set-initial-password ${NEO4J_PASSWORD:-change-me}
   ```
3. 启动：`<neo4j-home>/bin/neo4j.bat console`（Windows）或 `neo4j console`（Linux/macOS）

## 二、导入图谱

```bash
# 进入容器（方式 A）或本机 bin 目录（方式 B），依次执行 3 个 Cypher：
# 1. 建约束/索引（幂等）
cypher-shell -u neo4j -p ${NEO4J_PASSWORD:-change-me} -f /var/lib/neo4j/import/kg/00_setup.cypher
# 2. 导入节点
cypher-shell -u neo4j -p ${NEO4J_PASSWORD:-change-me} -f /var/lib/neo4j/import/kg/01_load_nodes.cypher
# 3. 导入关系
cypher-shell -u neo4j -p ${NEO4J_PASSWORD:-change-me} -f /var/lib/neo4j/import/kg/02_load_rels.cypher
```

> docker-compose 已将 `knowledge_graph_output/04_import/` 挂载到容器
> `/var/lib/neo4j/import/kg`（只读），Cypher 里 `file:///kg/...` 即指向该目录。

导入后验证：

```bash
cypher-shell -u neo4j -p ${NEO4J_PASSWORD:-change-me} "MATCH (n) RETURN labels(n)[0] AS l, count(*) ORDER BY l"
# 期望：222 节点（Category 10 / Product 8 / SKU 12 / Attribute 51 /
#        Policy 9 / Script 52 / FAQ 63 / Rule 17）
cypher-shell -u neo4j -p ${NEO4J_PASSWORD:-change-me} "MATCH ()-[r]->() RETURN type(r) AS t, count(*) ORDER BY t"
# 期望：240 关系（BELONGS_TO 19 / HAS_ATTR 51 / APPLIES_TO 36 /
#        REFERS_TO 65 / RELATED_TO 69）
```

## 三、环境变量（应用连接参数）

应用通过 `config.Settings.neo4j_*`（`from_env` 读 `NEO4J_URI` / `NEO4J_USER` /
`NEO4J_PASSWORD`）连接 Neo4j。默认值见 `.env.example`：

```bash
NEO4J_URI=http://localhost:7474
NEO4J_USER=neo4j
NEO4J_PASSWORD=change-me   # 请改成你的实际密码，勿提交真实密码
```

## 四、重新导出图谱可视化

数据更新后，重跑导出脚本生成 `knowledge_graph_output/knowledge_graph.html`：

```bash
.venv/Scripts/python.exe -m ecommerce_agent.knowledge_engine.export_graph knowledge_graph_output/knowledge_graph.html
```

## 五、常见问题

| 问题 | 处理 |
|---|---|
| `Connection refused` | Neo4j 未启动；`docker compose ps` 确认容器 running |
| `Auth failed` | 密码不对；方式 A 改 `NEO4J_AUTH` 后 `docker compose up -d` 重建 |
| 导入 CSV 找不到 | 确认 `04_import/` 挂载成功；方式 B 把 CSV 拷到 `<neo4j-home>/import/kg/` |
| 唯一约束冲突 | 重复导入会幂等跳过（MERGE）；若需清库：`MATCH (n) DETACH DELETE n` |
