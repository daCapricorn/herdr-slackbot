"""Validate Graphify output, save queries/provenance, and bundle offline HTML assets."""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
from pathlib import Path
import re
import subprocess


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--graphify", required=True, type=Path)
    parser.add_argument("--assets", required=True, type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    out = Path(__file__).parent / "graphify-out"
    graph_path = out / "graph.json"
    graph = json.loads(graph_path.read_text())
    nodes, links = graph["nodes"], graph["links"]
    ids = {n["id"] for n in nodes}
    assert len(ids) == len(nodes), "duplicate node IDs"
    assert all(e["source"] in ids and e["target"] in ids for e in links), "dangling edges"
    covered = {n.get("source_file") for n in nodes}
    source = sorted(root.glob("herdr_slackbot/*.py"))
    missing = [str(p.relative_to(root)) for p in source if str(p.relative_to(root)) not in covered]
    assert not missing, f"uncovered Python source files: {missing}"
    commands = {
        "bridge": ["explain", "herdr_slackbot_bridge_bridge"],
        "events": ["explain", "herdr_slackbot_events_subscriptionmanager"],
        "state": ["explain", "herdr_slackbot_state_statestore"],
        "navigation-path": ["path", "herdr_slackbot_slack_app_build_app", "herdr_slackbot_herdr_client_herdrclient", "--undirected"],
        "edge-diagnostic": ["diagnose", "multigraph", "--max-examples", "2"],
    }
    for name, command in commands.items():
        result = subprocess.run([str(args.graphify), *command, "--graph", str(graph_path)],
                                cwd=root, check=True, capture_output=True, text=True)
        (out / f"query-{name}.txt").write_text(result.stdout)
    bundled = {}
    for page, asset, pattern in (
        ("graph.html", "vis-network.min.js", r'<script\s+src="https://unpkg.com/vis-network[^>]*></script>'),
        ("GRAPH_TREE.html", "d3.v7.min.js", r'<script\s+src="https://d3js.org/d3.v7.min.js"[^>]*></script>'),
    ):
        path = out / page
        original = path.read_text()
        script = (args.assets / asset).read_text()
        assert "</script" not in script.lower(), "unsafe inline script boundary"
        updated, count = re.subn(pattern, lambda _: "<script>\n" + script + "\n</script>", original)
        if count:
            assert count == 1
            path.write_text(updated)
        else:
            assert script in original, f"missing expected asset in {page}"
        bundled[asset] = hashlib.sha256(script.encode()).hexdigest()
    pairs = {tuple(sorted((e["source"], e["target"]))) for e in links}
    metadata = {
        "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
        "graphify_version": "0.9.82",
        "mode": "code-only, local AST, no model API",
        "nodes": len(nodes), "raw_relations": len(links),
        "undirected_endpoint_pairs": len(pairs),
        "communities": len({n.get("community") for n in nodes}),
        "raw_relation_confidence": dict(collections.Counter(e.get("confidence") for e in links)),
        "python_source_files": len(source), "python_source_covered": len(source) - len(missing),
        "test_files_covered": len({f for f in covered if f and f.startswith("tests/")}),
        "source_sha256": {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in source},
        "offline_assets_sha256": bundled,
        "limitations": ["Static graph is navigation, not proof of runtime calls.",
                        "Clustering collapses same-endpoint relations; raw links remain in JSON.",
                        "Docs, images, fixture JSONL and plugin manifest were not semantically indexed.",
                        "HTML scripts bundled; browser rendering not verified this run."],
    }
    (out / "provenance.json").write_text(json.dumps(metadata, indent=2) + "\n")
    print(json.dumps({k: v for k, v in metadata.items() if k in (
        "nodes", "raw_relations", "undirected_endpoint_pairs", "communities",
        "python_source_files", "python_source_covered", "test_files_covered")}, indent=2))


if __name__ == "__main__":
    main()
