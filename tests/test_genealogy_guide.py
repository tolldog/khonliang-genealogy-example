"""genealogy_guide must be REGISTERED as a tool, not just advertised.

GenealogyMCPServer.__init__ calls add_guide("genealogy_guide", ...), which only
lists the guide in the catalog. Without a matching @app.tool() registration the
catalog would point at a tool that doesn't exist. This pins both halves.
"""

from types import SimpleNamespace

from khonliang.mcp.server import KhonliangMCPServer

from genealogy_agent.mcp_server import GenealogyMCPServer


class _FakeApp:
    """Captures @app.tool() registrations (FastMCP isn't installed in tests)."""

    def __init__(self):
        self.registered = {}
        self.resources = {}

    def tool(self):
        def deco(fn):
            self.registered[fn.__name__] = fn
            return fn
        return deco

    def resource(self, uri):
        def deco(fn):
            self.resources[uri] = fn
            return fn
        return deco


def _server():
    # Fully-configured: every optional component present (truthy stubs), so the
    # guide advertises the full tool set. Closures never call the stubs at
    # registration time.
    return GenealogyMCPServer(
        tree=SimpleNamespace(), forest=SimpleNamespace(),
        cross_matcher=SimpleNamespace(), importer=SimpleNamespace(),
        feedback_store=SimpleNamespace(), heuristic_pool=SimpleNamespace(),
        personality_registry=SimpleNamespace(),
    )


def test_guide_is_advertised_in_catalog():
    srv = _server()
    assert "genealogy_guide" in srv.guide_tools


def test_create_app_registers_the_guide_tool(monkeypatch):
    srv = _server()
    fake = _FakeApp()
    # bypass FastMCP: super().create_app() returns our fake, then the
    # GenealogyMCPServer._register_* methods register onto it.
    monkeypatch.setattr(KhonliangMCPServer, "create_app", lambda self: fake)

    srv.create_app()

    assert "genealogy_guide" in fake.registered, (
        "add_guide advertised genealogy_guide but create_app didn't register the tool"
    )
    # and it serves real content per topic
    out = fake.registered["genealogy_guide"](topic="evidence")
    assert "Evidence standards" in out


def test_guide_sections_cover_all_topics_and_are_nonempty():
    secs = _server()._genealogy_guide_sections()
    assert set(secs) >= {"workflow", "evidence", "tools", "matching"}
    assert all(v.strip() for v in secs.values())


def test_guide_omits_tools_not_registered_on_a_minimal_server():
    # tree-only server: forest / matching / import / training tools are NOT
    # registered, so the guide must not advertise them (codex: avoid pointing
    # clients at tools that don't exist).
    srv = GenealogyMCPServer(tree=SimpleNamespace())  # all optionals None
    secs = srv._genealogy_guide_sections()
    assert "matching" not in secs  # matching topic only when cross_matcher+forest
    blob = secs["workflow"] + secs["tools"]
    for absent in ("forest_list", "forest_search", "match_scan", "match_confirm",
                   "import_gedcom", "export_gedcom", "feedback_stats",
                   "heuristic_list", "personality_list"):
        assert absent not in blob, f"guide advertises unregistered tool: {absent}"
    assert "tree_search" in secs["tools"]  # tree tools always present


def test_guide_unknown_topic_falls_back_to_workflow():
    srv = _server()
    fake = _FakeApp()
    srv._register_genealogy_guide(fake)
    out = fake.registered["genealogy_guide"](topic="nope")
    assert "Unknown topic" in out and "workflow" in out.lower()
