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
    # Only `tree` is required; the tool closures reference the stores but never
    # call them at registration time, so stubs suffice.
    return GenealogyMCPServer(
        tree=SimpleNamespace(), forest=SimpleNamespace(),
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
    secs = GenealogyMCPServer._genealogy_guide_sections()
    assert set(secs) >= {"workflow", "evidence", "tools", "matching"}
    assert all(v.strip() for v in secs.values())


def test_guide_unknown_topic_falls_back_to_workflow():
    srv = _server()
    fake = _FakeApp()
    srv._register_genealogy_guide(fake)
    out = fake.registered["genealogy_guide"](topic="nope")
    assert "Unknown topic" in out and "workflow" in out.lower()
