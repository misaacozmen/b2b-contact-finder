"""Architectural boundary, dependency graph cycle detection, and callback integration tests."""

from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import config
import main
from modules import (
    api_configuration,
    checkpoint,
    discovery_rules,
    excel,
    output_artifacts,
    pipeline_runner,
    resolution_orchestrator,
    result_factory,
    run_budget,
    runtime_paths,
    search,
    secrets_store,
)


def _build_local_import_graph() -> dict[str, set[str]]:
    """Build a directed graph of imports among local modules."""
    root = Path(__file__).resolve().parent.parent
    graph: dict[str, set[str]] = {}

    local_files: list[Path] = [
        root / "main.py",
        root / "setup_company_resolvers.py",
        *sorted((root / "modules").glob("*.py")),
    ]

    all_modules = {
        f.stem if f.parent.name != "modules" else f"modules.{f.stem}"
        for f in local_files
    }
    all_modules.add("config")

    for file_path in local_files:
        if not file_path.exists():
            continue
        mod_name = file_path.stem if file_path.parent.name != "modules" else f"modules.{file_path.stem}"
        graph.setdefault(mod_name, set())
        tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.name
                    if name in all_modules or f"modules.{name}" in all_modules:
                        graph[mod_name].add(name if name in all_modules else f"modules.{name}")
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    if node.module == "modules":
                        for alias in node.names:
                            target = f"modules.{alias.name}"
                            if target in all_modules:
                                graph[mod_name].add(target)
                    elif node.module in all_modules:
                        graph[mod_name].add(node.module)
                    elif f"modules.{node.module}" in all_modules:
                        graph[mod_name].add(f"modules.{node.module}")
    return graph


def _find_cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    """Find cycles in directed graph using DFS."""
    visited: set[str] = set()
    stack: list[str] = []
    in_stack: set[str] = set()
    cycles: list[list[str]] = []

    def dfs(node: str) -> None:
        visited.add(node)
        stack.append(node)
        in_stack.add(node)

        for neighbor in graph.get(node, ()):
            if neighbor not in visited:
                dfs(neighbor)
            elif neighbor in in_stack:
                cycle_start = stack.index(neighbor)
                cycle = stack[cycle_start:] + [neighbor]
                cycles.append(cycle)

        stack.pop()
        in_stack.remove(node)

    for node in sorted(graph):
        if node not in visited:
            dfs(node)
    return cycles


class ModuleBoundariesTests(unittest.TestCase):
    def test_no_import_cycles(self):
        graph = _build_local_import_graph()
        cycles = _find_cycles(graph)
        cycle_descriptions = [" -> ".join(c) for c in cycles]
        self.assertEqual(
            cycles,
            [],
            f"Detected local import cycles: {cycle_descriptions}",
        )

    def test_no_submodule_imports_main(self):
        root = Path(__file__).resolve().parent.parent
        for mod_file in sorted((root / "modules").glob("*.py")):
            tree = ast.parse(mod_file.read_text(encoding="utf-8"), filename=str(mod_file))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertNotEqual(
                            alias.name,
                            "main",
                            f"{mod_file.name} illegally imports 'main' at line {node.lineno}",
                        )
                elif isinstance(node, ast.ImportFrom):
                    self.assertNotEqual(
                        node.module,
                        "main",
                        f"{mod_file.name} illegally imports from 'main' at line {node.lineno}",
                    )

    def test_discovery_rules_purity(self):
        file_path = Path("modules/discovery_rules.py")
        tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
        forbidden = {"search", "main", "crawler", "requests", "socket"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn(
                        alias.name,
                        forbidden,
                        f"discovery_rules illegally imports {alias.name}",
                    )
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    for name in forbidden:
                        self.assertNotIn(
                            name,
                            node.module.split("."),
                            f"discovery_rules illegally imports from {node.module}",
                        )

    def test_api_configuration_has_no_budget_state(self):
        file_path = Path("modules/api_configuration.py")
        tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
        defined_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                defined_names.add(node.name)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                defined_names.add(node.id)

        forbidden_names = {
            "_RUN_PAID_QUERY_LIMIT",
            "configure_run_budget",
            "scale_paid_api_budgets",
            "effective_paid_query_limit",
        }
        intersection = defined_names.intersection(forbidden_names)
        self.assertEqual(
            intersection,
            set(),
            f"api_configuration still carries budget state/functions: {intersection}",
        )

    def test_output_artifacts_has_no_orchestration(self):
        file_path = Path("modules/output_artifacts.py")
        tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
        defined_funcs = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
        self.assertNotIn("complete_resolution_evidence", defined_funcs)

        imported_modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported_modules.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module == "modules":
                    for alias in node.names:
                        imported_modules.add(alias.name)
                elif node.module:
                    imported_modules.add(node.module)

        forbidden_imports = {"search", "entity_resolution", "evidence_acquisition"}
        intersection = imported_modules.intersection(forbidden_imports)
        self.assertEqual(
            intersection,
            set(),
            f"output_artifacts still imports orchestration modules: {intersection}",
        )

    def test_pipeline_runner_has_no_empty_result_definition(self):
        file_path = Path("modules/pipeline_runner.py")
        tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
        defined_funcs = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
        self.assertNotIn("_empty_result", defined_funcs)
        self.assertNotIn("empty_result", defined_funcs)

    def test_main_run_callback_identities(self):
        with patch.object(pipeline_runner, "run_pipeline") as mock_run_pipeline:
            mock_run_pipeline.return_value = "report"
            main._run(Path("firms.xlsx"))
            mock_run_pipeline.assert_called_once()
            kwargs = mock_run_pipeline.call_args.kwargs
            self.assertIs(kwargs["process_company_fn"], main.process_company)
            self.assertIs(kwargs["write_outputs_fn"], main._write_outputs)
            self.assertIs(kwargs["set_output_dir_fn"], main._set_output_dir)
            self.assertIs(kwargs["empty_result_fn"], main._empty_result)

    def test_main_contact_output_fields_monkeypatch_chain(self):
        """Monkeypatching main._contact_output_fields must alter evaluation evidence contacts."""
        dummy_evaluation = {
            "candidate": {"url": "https://test.com"},
            "final_score": 80,
            "email": "orig@test.com",
            "phone": "+902120000000",
        }
        with patch.object(
            main,
            "_contact_output_fields",
            return_value={"custom_contacts_field": "patched_value"},
        ):
            evidence = main._evaluation_evidence(dummy_evaluation)
            self.assertEqual(
                evidence["contacts"],
                {"custom_contacts_field": "patched_value"},
            )

    def test_search_candidate_role_monkeypatch_chain(self):
        """Monkeypatching search._candidate_role must affect bridge candidate extraction."""
        target: dict[str, dict] = {}
        sample_results = [
            {
                "url": "https://directory.com/firm/profile",
                "title": "Acme Directory Profile",
                "body": "Acme industrial supplier details",
            }
        ]
        with patch.object(search, "_candidate_role", return_value="directory"), patch.object(
            search, "_bridge_identity_supported", return_value=True
        ):
            search._collect_search_bridge_sources(
                target,
                "Acme Corp",
                "query",
                sample_results,
                metadata=None,
            )
            self.assertIn("https://directory.com/firm/profile", target)
            self.assertEqual(
                target["https://directory.com/firm/profile"]["role"],
                "directory",
            )

    def test_pipeline_runner_empty_result_monkeypatch_chain(self):
        """Monkeypatching main._empty_result must produce patched row on worker failure."""
        with tempfile.TemporaryDirectory() as tmpdir:
            input_file = Path(tmpdir) / "firms.xlsx"
            excel.write_company_records(input_file, [{"company": "Failing Corp"}])

            with patch.object(
                main, "process_company", side_effect=RuntimeError("Simulated worker error")
            ), patch.object(
                main,
                "_empty_result",
                return_value={"company": "Failing Corp", "status": "PATCHED_FAILED_STATUS"},
            ), patch.object(
                pipeline_runner, "ensure_directories"
            ), patch.object(
                pipeline_runner, "setup_logging"
            ):
                with patch.object(pipeline_runner, "deduplicate_company_records", return_value=([{"company": "Failing Corp"}], 0)):
                    with patch.object(checkpoint, "load_progress", return_value=None), patch.object(
                        checkpoint, "save_result"
                    ), patch.object(
                        checkpoint, "clear_run_progress"
                    ):
                        written_rows: list[dict] = []
                        def capture_outputs(rows, elapsed):
                            written_rows.extend(rows)
                            return "ok"

                        pipeline_runner.run_pipeline(
                            input_file,
                            process_company_fn=main.process_company,
                            write_outputs_fn=capture_outputs,
                            set_output_dir_fn=lambda p: None,
                            empty_result_fn=main._empty_result,
                        )
                        self.assertEqual(len(written_rows), 1)
                        self.assertEqual(written_rows[0]["status"], "PATCHED_FAILED_STATUS")

    def test_search_query_priority_monkeypatch_chain(self):
        """Monkeypatching search._query_priority alters trust bonus, primary and fallback query ranking."""
        with patch.object(search, "_query_priority", return_value=3):
            self.assertEqual(
                search._query_trust_bonus("arbitrary query"),
                config.TARGET_COUNTRY_OFFICIAL_QUERY_BONUS,
            )
            p_queries = search._primary_queries("Acme Corp", None)
            self.assertTrue(len(p_queries) > 0)
            f_queries = search._fallback_queries("Acme Corp", None)
            self.assertTrue(len(f_queries) > 0)

    def test_search_metadata_query_terms_monkeypatch_chain(self):
        """Monkeypatching search._metadata_query_terms alters query planner inputs."""
        with patch.object(search, "_metadata_query_terms", return_value=["CUSTOM_CONTEXT_TERM"]):
            p_queries = search._primary_queries("Acme Corp", {"sector": "plastics"})
            self.assertTrue(any("CUSTOM_CONTEXT_TERM" in q for q in p_queries))
            f_queries = search._fallback_queries("Acme Corp", {"sector": "plastics"})
            self.assertTrue(any("CUSTOM_CONTEXT_TERM" in q for q in f_queries))
            a_queries = search._adaptive_queries(
                "Acme Corp", {"sector": "plastics"}, evidence_gaps={"ambiguous_candidates"}
            )
            self.assertTrue(any("CUSTOM_CONTEXT_TERM" in q for q in a_queries))

    def test_search_candidate_rank_key_monkeypatch_chain(self):
        """Monkeypatching search._candidate_rank_key alters ranking, search control key and best candidate."""
        c1 = {"url": "https://a.com", "domain": "a.com", "score": 90, "role": "company_candidate"}
        c2 = {"url": "https://b.com", "domain": "b.com", "score": 40, "role": "company_candidate"}
        candidates = {"a.com": c1, "b.com": c2}
        with patch.object(
            search,
            "_candidate_rank_key",
            side_effect=lambda item: (1 if item["domain"] == "b.com" else 0,),
        ):
            best = search._best_candidate(candidates)
            self.assertIsNotNone(best)
            self.assertEqual(best["domain"], "b.com")
            control_key = search._candidate_search_control_key(c2)
            self.assertEqual(control_key[0], 1)

    def test_search_canonical_site_url_monkeypatch_chain(self):
        """Monkeypatching search._canonical_site_url alters outbound site normalization."""
        result = {"body": "Visit our website: https://example.com/subpage"}
        with patch.object(search, "_canonical_site_url", return_value="https://custom-canonical.com"):
            outbound = search._snippet_outbound_websites(result, "https://thirdparty.com/profile")
            self.assertEqual(outbound, ["https://custom-canonical.com"])

    def test_search_can_early_stop_monkeypatch_chain(self):
        """Monkeypatching search._can_early_stop alters discovery expansion decision."""
        candidates = {
            "example.com": {
                "url": "https://example.com",
                "domain": "example.com",
                "score": config.EARLY_STOP_SCORE_THRESHOLD + 10,
                "role": "company_candidate",
            }
        }
        with patch.object(search, "_can_early_stop", return_value=False):
            self.assertTrue(search._discovery_needs_expansion("Example Corp", candidates, None))
        with patch.object(search, "_can_early_stop", return_value=True):
            self.assertFalse(search._discovery_needs_expansion("Example Corp", candidates, None))

    def test_search_bridge_anchor_supported_monkeypatch_chain(self):
        """Monkeypatching search._bridge_entity_anchor_supported alters bridge identity verification."""
        with patch.object(search, "_bridge_entity_anchor_supported", return_value=False):
            self.assertFalse(
                search._bridge_identity_supported("Example Corp", "Title", "Snippet", None, "https://dir.com")
            )
        with patch.object(search, "_bridge_entity_anchor_supported", return_value=True):
            self.assertTrue(
                search._bridge_identity_supported("Example Corp", "Example Corp", "Snippet", None, "https://dir.com")
            )

    def test_search_role_priority_monkeypatch_chain(self):
        """Monkeypatching search._ROLE_PRIORITY alters strongest candidate role selection."""
        custom_priority = {"news": 100, "fair_profile": 10}
        with patch.object(search, "_ROLE_PRIORITY", custom_priority):
            self.assertEqual(
                search._strongest_candidate_role("news", "fair_profile"),
                "news",
            )

    def test_offline_network_guard_active(self):
        import os
        import socket
        import urllib.request
        if os.environ.get("B2B_TEST_OFFLINE") == "1":
            # 1. TCP socket creation
            with self.assertRaises(RuntimeError) as ctx:
                socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.assertIn("Network access disabled during tests", str(ctx.exception))

            # 2. UDP socket creation
            with self.assertRaises(RuntimeError) as ctx:
                socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.assertIn("Network access disabled during tests", str(ctx.exception))

            # 3. getaddrinfo
            with self.assertRaises(RuntimeError) as ctx:
                socket.getaddrinfo("example.com", 80)
            self.assertIn("Network access disabled during tests", str(ctx.exception))

            # 4. gethostbyname_ex
            with self.assertRaises(RuntimeError) as ctx:
                socket.gethostbyname_ex("example.com")
            self.assertIn("Network access disabled during tests", str(ctx.exception))

            # 5. getnameinfo
            with self.assertRaises(RuntimeError) as ctx:
                socket.getnameinfo(("127.0.0.1", 80), 0)
            self.assertIn("Network access disabled during tests", str(ctx.exception))

            # 6. urllib.request.urlopen
            with self.assertRaises(RuntimeError) as ctx:
                urllib.request.urlopen("https://example.com")
            self.assertIn("Network access disabled during tests", str(ctx.exception))

    def test_redaction_scanner_module_boundaries(self):
        graph = _build_local_import_graph()
        scanner_imports = graph.get("modules.redaction_scanner", set())
        self.assertNotIn("main", scanner_imports)
        self.assertNotIn("modules.search", scanner_imports)
        self.assertNotIn("config", scanner_imports)
        self.assertNotIn("modules.cache_store", scanner_imports)
        self.assertNotIn("modules.replay_snapshot", scanner_imports)

        # Used unidirectionally by redaction
        self.assertIn("modules.redaction_scanner", graph.get("modules.redaction", set()))

        # No circular imports
        cycles = _find_cycles(graph)
        scanner_cycles = [c for c in cycles if "modules.redaction_scanner" in c]
        self.assertEqual(scanner_cycles, [])


if __name__ == "__main__":
    unittest.main()
