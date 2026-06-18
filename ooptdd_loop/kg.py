"""KG-native loop I/O — persist runs so coverage and drift become queries.

Once a run is persisted, "which requirements are DONE?" (coverage) and "which
Longinus bindings drifted?" (source sha256 changed vs its baseline) are answered
by querying the store — no re-run, no parsing logs again.

The store is pluggable, mirroring the backend design, so the **offline invariant
holds** (hard-core #3: KG is never a hard dependency):

* ``InMemoryKgStore`` — dict-backed; used by tests and offline runs.
* ``Neo4jKgStore``    — real graph; env-gated (``NEO4J_URI/USER/PASSWORD``).

Node shape (both stores agree):
  OoptddRequirement {spec, id, description}
  OoptddVerdict     {cid, spec, requirement_id, gate_ok, reachable, bound, done}
  ReferenceSite     {kg_anchor, sha256, sha256_baseline, blob_oid, blob_oid_baseline,
                     commit, repo_relpath, ...}                 (Longinus 7-tuple)
Drift = the anchor's content changed vs its baseline (baseline set on first sight,
current updated each run). When the binding carries a git ``blob_oid`` (the
content-addressed, machine-independent signal) drift is ``blob_oid != blob_oid_baseline``;
otherwise it falls back to ``sha256 != sha256_baseline`` — the Longinus v3.3 definition.
Anchoring on the git blob makes drift reproducible across clones: the baseline is tied
to a real git object, not to one machine's working copy.
"""
from __future__ import annotations

import os
from typing import Protocol, runtime_checkable


def _drifted(s: dict) -> bool:
    """Has this ReferenceSite's content moved off its baseline? Prefer the git
    ``blob_oid`` (content-addressed, reproducible across clones) when both baseline and
    current are present; otherwise fall back to the plain ``sha256`` baseline."""
    bb, bc = s.get("blob_baseline"), s.get("blob_current")
    if bb and bc:
        return bc != bb
    return s["current"] != s["baseline"]


def _verdict_rows(cid: str, spec_name: str, results) -> list[dict]:
    return [
        {"cid": cid, "spec": spec_name, "requirement_id": r.id,
         "gate_ok": r.gate_ok, "reachable": r.reachable, "bound": r.bound, "done": r.done}
        for r in results
    ]


@runtime_checkable
class KgStore(Protocol):
    def write_run(self, cid: str, spec_name: str, results) -> None: ...
    def coverage(self, spec_name: str) -> dict: ...
    def drift(self, spec_name: str) -> list[dict]: ...


class InMemoryKgStore:
    """Graph-shaped dict store: proves the queries with zero infrastructure."""

    def __init__(self):
        self._runs: dict[str, list[tuple[str, list[dict]]]] = {}
        # spec -> anchor -> {baseline, current, source_path, symbol}
        self._sites: dict[str, dict[str, dict]] = {}

    def write_run(self, cid: str, spec_name: str, results) -> None:
        self._runs.setdefault(spec_name, []).append((cid, _verdict_rows(cid, spec_name, results)))
        sites = self._sites.setdefault(spec_name, {})
        for r in results:
            b = getattr(r, "binding", None)
            if b is None or not getattr(b, "sha256", ""):
                continue
            blob = getattr(b, "blob_oid", None)
            cur = sites.get(b.kg_anchor)
            if cur is None:
                sites[b.kg_anchor] = {
                    "baseline": b.sha256, "current": b.sha256,
                    "blob_baseline": blob, "blob_current": blob,
                    "source_path": b.source_path, "symbol": b.symbol,
                    "commit": getattr(b, "commit", None),
                    "repo_relpath": getattr(b, "repo_relpath", None),
                }
            else:
                cur["current"] = b.sha256
                cur["blob_current"] = blob
                cur["commit"] = getattr(b, "commit", None)

    def coverage(self, spec_name: str) -> dict:
        runs = self._runs.get(spec_name) or []
        if not runs:
            return {"spec": spec_name, "runs": 0, "done": 0, "total": 0, "incomplete": []}
        cid, rows = runs[-1]  # latest run
        done = sum(1 for v in rows if v["done"])
        return {"spec": spec_name, "runs": len(runs), "cid": cid, "done": done,
                "total": len(rows), "complete": done == len(rows),
                "incomplete": [v["requirement_id"] for v in rows if not v["done"]]}

    def drift(self, spec_name: str) -> list[dict]:
        out = []
        for anchor, s in (self._sites.get(spec_name) or {}).items():
            if _drifted(s):
                out.append({"kg_anchor": anchor, "source_path": s["source_path"],
                            "symbol": s["symbol"], "baseline": s["baseline"],
                            "current": s["current"],
                            "blob_baseline": s.get("blob_baseline"),
                            "blob_current": s.get("blob_current"),
                            "commit": s.get("commit"),
                            "repo_relpath": s.get("repo_relpath")})
        return out


class Neo4jKgStore:
    """Production store. Env-gated; raises if NEO4J_* / the driver are absent."""

    def __init__(self, uri: str | None = None, user: str | None = None,
                 password: str | None = None):
        uri = uri or os.getenv("NEO4J_URI") or os.getenv("NEO4J_URL")
        if not uri:
            raise RuntimeError("Neo4jKgStore needs NEO4J_URI (env)")
        from neo4j import GraphDatabase  # optional dependency (extra `kg`)

        self._drv = GraphDatabase.driver(
            uri, auth=(user or os.getenv("NEO4J_USER", "neo4j"),
                       password or os.getenv("NEO4J_PASSWORD", "")))

    def close(self):
        self._drv.close()

    def write_run(self, cid: str, spec_name: str, results) -> None:
        rows = _verdict_rows(cid, spec_name, results)
        sites = [
            {"kg_anchor": r.binding.kg_anchor, "sha256": r.binding.sha256,
             "source_path": r.binding.source_path, "symbol": r.binding.symbol,
             "blob_oid": getattr(r.binding, "blob_oid", None),
             "commit": getattr(r.binding, "commit", None),
             "remote": getattr(r.binding, "remote", None),
             "repo_relpath": getattr(r.binding, "repo_relpath", None)}
            for r in results if getattr(r, "binding", None) and r.binding.sha256
        ]
        with self._drv.session() as s:
            s.run(
                """UNWIND $rows AS v
                   MERGE (req:OoptddRequirement {spec:v.spec, id:v.requirement_id})
                   MERGE (vd:OoptddVerdict {cid:v.cid, spec:v.spec, requirement_id:v.requirement_id})
                   SET vd.gate_ok=v.gate_ok, vd.reachable=v.reachable, vd.bound=v.bound,
                       vd.done=v.done, vd.at=datetime()
                   MERGE (vd)-[:FOR_REQUIREMENT]->(req)""", rows=rows)
            s.run(
                """UNWIND $sites AS x
                   MERGE (rs:ReferenceSite:Longinus {kg_anchor:x.kg_anchor})
                   ON CREATE SET rs.sha256_baseline=x.sha256, rs.blob_oid_baseline=x.blob_oid
                   SET rs.sha256=x.sha256, rs.source_path=x.source_path, rs.symbol=x.symbol,
                       rs.blob_oid=x.blob_oid, rs.commit=x.commit, rs.remote=x.remote,
                       rs.repo_relpath=x.repo_relpath, rs.last_validated=datetime()""", sites=sites)

    def coverage(self, spec_name: str) -> dict:
        with self._drv.session() as s:
            rec = s.run(
                """MATCH (vd:OoptddVerdict {spec:$spec})
                   WITH vd ORDER BY vd.at DESC
                   WITH vd.requirement_id AS rid, head(collect(vd)) AS latest
                   RETURN count(*) AS total, sum(CASE WHEN latest.done THEN 1 ELSE 0 END) AS done,
                          collect(CASE WHEN NOT latest.done THEN rid END) AS incomplete""",
                spec=spec_name).single()
        if rec is None or rec["total"] == 0:
            return {"spec": spec_name, "done": 0, "total": 0, "incomplete": []}
        inc = [x for x in rec["incomplete"] if x]
        return {"spec": spec_name, "done": rec["done"], "total": rec["total"],
                "complete": rec["done"] == rec["total"], "incomplete": inc}

    def drift(self, spec_name: str) -> list[dict]:
        # Prefer the git blob_oid (content-addressed, reproducible across clones) when both
        # baseline and current exist; otherwise fall back to the sha256 baseline.
        with self._drv.session() as s:
            return [dict(r) for r in s.run(
                """MATCH (rs:ReferenceSite)
                   WHERE (rs.blob_oid IS NOT NULL AND rs.blob_oid_baseline IS NOT NULL
                          AND rs.blob_oid <> rs.blob_oid_baseline)
                      OR ((rs.blob_oid IS NULL OR rs.blob_oid_baseline IS NULL)
                          AND rs.sha256 <> rs.sha256_baseline)
                   RETURN rs.kg_anchor AS kg_anchor, rs.source_path AS source_path,
                          rs.symbol AS symbol, rs.sha256_baseline AS baseline,
                          rs.sha256 AS current, rs.blob_oid_baseline AS blob_baseline,
                          rs.blob_oid AS blob_current, rs.commit AS commit,
                          rs.repo_relpath AS repo_relpath""")]
