# KG: OOPTDD_methodology_v1
"""Seed the OOPTDD methodology rules into Neo4j as abstract KG nodes."""
from __future__ import annotations

import os
from dataclasses import asdict
from typing import Any

from .rules import OOPTDD_METHOD_NAME, OOPTDD_METHOD_TITLE, canonical_rules


def seed_payload() -> dict[str, Any]:
    rules = [asdict(r) for r in canonical_rules()]
    rule_name_by_id = {r.id: r.name for r in canonical_rules()}
    deps = [
        {"from": r.name, "to": rule_name_by_id[dep_id]}
        for r in canonical_rules()
        for dep_id in r.depends_on
    ]
    return {
        "method": {
            "name": OOPTDD_METHOD_NAME,
            "title": OOPTDD_METHOD_TITLE,
            "status": "CANONICAL",
            "version": "1.0.0",
            "summary": (
                "OOPTDD absorbs LTDD/log-positive TDD, outside-in role discovery, "
                "message contracts, integration backstops, and Longinus KG binding."
            ),
        },
        "rules": rules,
        "dependencies": deps,
        "absorbs": ["LTDD_methodology_v1"],
    }


def seed_cypher() -> str:
    return """
MERGE (m:AbstractNode:Methodology {name:$method.name})
SET m.title = $method.title,
    m.status = $method.status,
    m.version = $method.version,
    m.summary = $method.summary,
    m.updated_at = datetime()
WITH m
UNWIND $rules AS r
MERGE (rule:AbstractNode:OOPTDDRule {name:r.name})
SET rule.rule_id = r.id,
    rule.title = r.title,
    rule.category = r.category,
    rule.statement = r.statement,
    rule.severity = r.severity,
    rule.status = 'CANONICAL',
    rule.updated_at = datetime()
MERGE (m)-[:HAS_RULE]->(rule)
WITH m
UNWIND $absorbs AS absorbed_name
OPTIONAL MATCH (absorbed {name: absorbed_name})
FOREACH (_ IN CASE WHEN absorbed IS NULL THEN [] ELSE [1] END |
  MERGE (m)-[:ABSORBS]->(absorbed)
)
WITH m
UNWIND $dependencies AS dep
MATCH (a:OOPTDDRule {name:dep.from})
MATCH (b:OOPTDDRule {name:dep.to})
MERGE (a)-[:DEPENDS_ON]->(b)
RETURN m.name AS methodology, count(*) AS dependency_edges
""".strip()


def write_seed(uri: str | None = None, user: str | None = None,
               password: str | None = None) -> bool:
    """Write the seed through the Neo4j Python driver when env is configured."""
    uri = uri or os.getenv("NEO4J_URI") or os.getenv("LONGINUS_NEO4J_URI")
    if not uri:
        return False
    user = user or os.getenv("NEO4J_USER") or os.getenv("LONGINUS_NEO4J_USER") or "neo4j"
    password = (
        password
        if password is not None
        else os.getenv("NEO4J_PASSWORD") or os.getenv("LONGINUS_NEO4J_PASSWORD") or ""
    )
    try:
        from neo4j import GraphDatabase
    except ImportError:
        return False
    try:
        driver = GraphDatabase.driver(uri, auth=(user, password))
        with driver.session() as session:
            session.run(seed_cypher(), **seed_payload()).consume()
        driver.close()
        return True
    except Exception:
        return False
