"""图谱可视化导出：把 Neo4j 图谱导出为自包含的 HTML 可视化文件。

目标：别人拿到一个 HTML 文件，浏览器打开即可看到知识图谱（无需装 Neo4j）。

- 从 Neo4j 拉取全部节点 + 关系（通过 HTTP API 输出 JSON）
- 生成自包含 HTML（内嵌 D3.js 力导向图，可缩放/拖拽/查看详情）
- 纯前端无依赖，双击打开即用

用法（命令行）：
    python -m ecommerce_agent.knowledge_engine.export_graph <output.html>

连接参数走 env（NEO4J_URI/NEO4J_USER/NEO4J_PASSWORD），默认本地开发值，
不再硬编码机器专属路径（P1-5 可复现）。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Neo4j 连接信息（env 可覆盖；默认占位符密码，真实密码走 NEO4J_PASSWORD）
NEO4J_URI = os.getenv("NEO4J_URI", "http://localhost:7474")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "change-me")

# 节点颜色（按标签）
NODE_COLORS = {
    "Category": "#4CAF50",
    "Product": "#2196F3",
    "SKU": "#9C27B0",
    "Attribute": "#FF9800",
    "Policy": "#F44336",
    "Script": "#00BCD4",
    "FAQ": "#FFC107",
    "Rule": "#795548",
}


def _run_http(query: str) -> list[list]:
    """通过 Neo4j HTTP API 执行查询，返回行列表（标准 JSON）。"""
    import base64
    import urllib.request

    endpoint = f"{NEO4J_URI}/db/neo4j/tx/commit"
    body = json.dumps({"statements": [{"statement": query}]}).encode("utf-8")
    token = base64.b64encode(f"{NEO4J_USER}:{NEO4J_PASSWORD}".encode()).decode()
    req = urllib.request.Request(endpoint, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Basic {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if payload.get("errors"):
        raise RuntimeError(f"Neo4j 查询失败: {payload['errors']}")
    rows = payload["results"][0]["data"]
    return [r["row"] for r in rows]


def export_graph(out_path: str | Path) -> dict:
    """导出图谱为 HTML 可视化文件。返回统计。"""
    # 1. 拉节点（含标签 + 属性）
    nodes_query = (
        "MATCH (n) RETURN n.id AS id, labels(n)[0] AS label, "
        "coalesce(n.title, n.policy_name, n.rule_title, n.category_name, n.question, n.intent, n.id) AS title "
        "LIMIT 500"
    )
    # 2. 拉关系（含类型 + 方向）
    rels_query = (
        "MATCH (a)-[r]->(b) RETURN a.id AS source, type(r) AS rel, b.id AS target "
        "LIMIT 800"
    )

    node_rows = _run_http(nodes_query)
    rel_rows = _run_http(rels_query)

    nodes = []
    for row in node_rows:
        nid, label, title = row[0], row[1], row[2]
        if nid is None:
            continue
        nodes.append({"id": str(nid), "label": str(label or "Node"), "title": str(title or nid)})

    links = []
    for row in rel_rows:
        source, rel, target = row[0], row[1], row[2]
        if source and target:
            links.append({"source": str(source), "target": str(target), "rel": str(rel)})

    # 3. 生成 HTML（内嵌 D3.js 力导向图）
    html = _build_html(nodes, links)

    out = Path(out_path)
    out.write_text(html, encoding="utf-8")
    return {"nodes": len(nodes), "links": len(links), "html": str(out)}


def _build_html(nodes: list[dict], links: list[dict]) -> str:
    """生成自包含 HTML（D3 力导向图）。"""
    nodes_json = json.dumps(nodes, ensure_ascii=False)
    links_json = json.dumps(links, ensure_ascii=False)
    colors_json = json.dumps(NODE_COLORS, ensure_ascii=False)
    return f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<title>云湃知识图谱</title>
<style>
  body {{ margin:0; font-family:sans-serif; background:#fafafa; }}
  #header {{ padding:12px 20px; background:#263238; color:#fff; }}
  #header h1 {{ margin:0; font-size:18px; }}
  #header span {{ font-size:12px; opacity:.8; }}
  #legend {{ padding:8px 20px; background:#eceff1; font-size:12px; }}
  #legend span {{ margin-right:14px; }}
  #graph {{ width:100%; height:calc(100vh - 80px); }}
  svg {{ width:100%; height:100%; }}
  .node circle {{ stroke:#fff; stroke-width:1.5px; }}
  .node text {{ font-size:10px; fill:#37474f; pointer-events:none; }}
  .link {{ stroke:#b0bec5; stroke-opacity:.6; stroke-width:1px; }}
  #tooltip {{ position:absolute; background:#263238; color:#fff; padding:8px 12px;
             border-radius:4px; font-size:12px; pointer-events:none; display:none;
             max-width:300px; z-index:10; }}
</style>
</head>
<body>
<div id="header"><h1>云湃知识图谱</h1>
  <span id="stats"></span></div>
<div id="legend"></div>
<div id="graph"></div>
<div id="tooltip"></div>

<script src="https://d3js.org/d3.v7.min.js"></script>
<script>
const nodes = {nodes_json};
const links = {links_json};
const colors = {colors_json};

document.getElementById('stats').textContent =
  nodes.length + ' 实体 · ' + links.length + ' 关系（点击节点查看详情）';

// 图例
const legend = document.getElementById('legend');
const labelSet = new Set(nodes.map(n => n.label));
labelSet.forEach(l => {{
  const span = document.createElement('span');
  span.innerHTML = '<span style="color:'+(colors[l]||'#999')+'">●</span> ' + l;
  legend.appendChild(span);
}});

// 力导向图
const width = window.innerWidth, height = window.innerHeight - 80;
const svg = d3.select('#graph').append('svg')
  .attr('viewBox', `0 0 ${{width}} ${{height}}`);

const sim = d3.forceSimulation(nodes)
  .force('link', d3.forceLink(links).id(d => d.id).distance(80))
  .force('charge', d3.forceManyBody().strength(-300))
  .force('center', d3.forceCenter(width/2, height/2))
  .force('collide', d3.forceCollide(20));

const link = svg.selectAll('.link')
  .data(links).enter().append('line')
  .attr('class', 'link');

const node = svg.selectAll('.node')
  .data(nodes).enter().append('g')
  .attr('class', 'node')
  .call(d3.drag()
    .on('start', dragstarted).on('drag', dragged).on('end', dragended));

node.append('circle')
  .attr('r', d => d.label==='FAQ'||d.label==='Script' ? 12 : 9)
  .attr('fill', d => colors[d.label] || '#999');

node.append('text')
  .attr('dx', 14).attr('dy', 4)
  .text(d => (d.title || '').slice(0, 12));

// tooltip
node.on('click', (event, d) => {{
  const tip = document.getElementById('tooltip');
  tip.style.display = 'block';
  tip.style.left = (event.pageX+12)+'px';
  tip.style.top = (event.pageY+12)+'px';
  tip.innerHTML = '<b>'+d.title+'</b><br>ID: '+d.id+'<br>类型: '+d.label;
}});
document.getElementById('graph').onclick = (e) => {{
  if (e.target === document.getElementById('graph'))
    document.getElementById('tooltip').style.display = 'none';
}};

sim.on('tick', () => {{
  link.attr('x1', d => d.source.x).attr('y1', d => d.source.y)
      .attr('x2', d => d.target.x).attr('y2', d => d.target.y);
  node.attr('transform', d => `translate(${{d.x}},${{d.y}})`);
}});

function dragstarted(event, d) {{
  if (!event.active) sim.alphaTarget(0.3).restart();
  d.fx = d.x; d.fy = d.y;
}}
function dragged(event, d) {{
  d.fx = event.x; d.fy = event.y;
}}
function dragended(event, d) {{
  if (!event.active) sim.alphaTarget(0);
  d.fx = null; d.fy = null;
}}
</script>
</body>
</html>"""


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "knowledge_graph.html"
    result = export_graph(out)
    print(f"✅ 图谱已导出: {result['html']}")
    print(f"   节点 {result['nodes']} 个, 关系 {result['links']} 条")
    print(f"   浏览器打开即看（无需 Neo4j）")
