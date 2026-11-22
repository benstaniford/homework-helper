"""Tests for the homework helper: /api/ask over a fake claude-broker, the MCP tools
socket, item text and the web tools. No network."""
import json
import os
import socket
import sys
import tempfile
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("SECRET_KEY", "test-secret")

import app as app_module  # noqa: E402
import homework_data  # noqa: E402
import tutor as tutor_module  # noqa: E402
import web_tools  # noqa: E402
from fake_broker import FakeBroker, FakeHTTPError  # noqa: E402
from mva_canvas.models import ModuleItem  # noqa: E402
from test_app import make_fake, _assignment  # noqa: E402


class ContentFake:
    """Adds the content calls item_text makes to test_app's FakeApi."""

    def __init__(self, fake):
        self.fake = fake
        self.client = SimpleNamespace(get=self._get)
        self.pages = {
            "https://canvas.example/api/v1/courses/1/pages/osmosis":
                {"title": "Osmosis notes", "body": '<p>Water moves <a href="https://vid.example/x">Watch this</a></p>'},
            "https://canvas.example/api/v1/courses/1/files/9":
                {"display_name": "sheet.docx", "content-type": "application/msword", "url": "https://dl/9"},
        }

    def __getattr__(self, name):
        return getattr(self.fake, name)

    def _get(self, url, params=None):
        return self.pages[url]

    def get_assignment(self, course_id, assignment_id):
        a = next(a for a in self.fake.assignments if a.id == assignment_id)
        a.description_html = f"<p>Brief for <b>{a.name}</b></p>"
        return a


def make_content_fake():
    fake = make_fake()
    fake.module.items += [
        ModuleItem(id=40, module_id=10, title="Osmosis notes", type="Page",
                   html_url="https://canvas.example/items/40", page_url="osmosis",
                   url="https://canvas.example/api/v1/courses/1/pages/osmosis"),
        ModuleItem(id=41, module_id=10, title="Worksheet", type="File", content_id=9,
                   html_url="https://canvas.example/items/41",
                   url="https://canvas.example/api/v1/courses/1/files/9"),
        ModuleItem(id=42, module_id=10, title="BBC clip", type="ExternalUrl",
                   html_url="", raw={"external_url": "https://bbc.example/clip"}),
    ]
    fake.assignments.append(_assignment(11, "SUBMIT: Finished one", submitted=True))
    fake.module.items.append(ModuleItem(id=43, module_id=10, title="SUBMIT: Finished one",
                                        type="Assignment", content_id=11))
    return ContentFake(fake)


# -- report keys and item text ---------------------------------------------------------

def test_report_keys_items_and_materials():
    rep = homework_data.build_report(make_content_fake())
    keys = rep.items_by_key
    assert keys["1:assignment:2"].title == "Soon worksheet"
    assert keys["1:assignment:1"].title == "Late essay"           # overdue
    assert keys["1:assignment:3"].title == "Later project"        # upcoming
    assert keys["1:page:osmosis"].material is True
    assert keys["1:file:9"].material is True
    assert keys["1:externalurl:42"].html_url == "https://bbc.example/clip"
    assert keys["1:assignment:11"].done is True
    # materials aren't shown as tasks
    assert "Osmosis notes" not in [i.title for g in rep.this_week for s in g.stages for i in s.items]
    assert rep.recent_items()[-1].material


def test_item_text():
    api = make_content_fake()
    rep = homework_data.build_report(api)
    text = homework_data.item_text(api, rep.items_by_key["1:assignment:2"])
    assert "Soon worksheet" in text and "Brief for Soon worksheet" in text and "Due:" in text
    page = homework_data.item_text(api, rep.items_by_key["1:page:osmosis"])
    assert "Water moves Watch this (https://vid.example/x)" in page
    assert "can't be read" in homework_data.item_text(api, rep.items_by_key["1:file:9"])
    assert "https://bbc.example/clip" in homework_data.item_text(api, rep.items_by_key["1:externalurl:42"])
    assert len(homework_data.item_text(api, rep.items_by_key["1:page:osmosis"], limit=20)) < 40


# -- /api/ask over a fake broker -----------------------------------------------------

@pytest.fixture
def broker_dir():
    # unix socket paths must be short: keep it out of pytest's deep tmp_path
    with tempfile.TemporaryDirectory(prefix="hwb", dir="/tmp") as d:
        yield d


@pytest.fixture
def setup(monkeypatch, broker_dir):
    api = make_content_fake()

    def login(u, p):
        if p != "right":
            from mva_canvas.errors import CanvasAuthError
            raise CanvasAuthError("bad")
        return api

    monkeypatch.setattr(app_module, "cache", homework_data.SessionCache(login=login))
    fake = FakeBroker(os.path.join(broker_dir, "broker.sock")).start()
    t = tutor_module.Tutor(lambda: app_module.cache, broker_dir=broker_dir)
    monkeypatch.setattr(app_module, "tutor", t)
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield SimpleNamespace(client=c, fake=fake, tutor=t, api=api)
    t.stop()
    fake.stop()


def _login(c, user="kid"):
    c.post("/login", data={"username": user, "password": "right"})


def _ask(c, **body):
    r = c.post("/api/ask", json=body)
    if r.mimetype != "application/x-ndjson":
        return r, None
    return r, [json.loads(line) for line in r.data.decode().splitlines() if line]


def _reply(answer="Try thinking about which way water moves.", resources=()):
    def reply(body):
        return [
            {"type": "tool", "name": "mcp__homework__read_item", "input": {"chat": "x", "key": "1:page:osmosis"}},
            {"type": "tool", "name": "mcp__homework__web_search", "input": {"query": "osmosis bitesize"}},
            {"type": "field", "name": "answer", "delta": answer[:10]},
            {"type": "field", "name": "answer", "delta": answer[10:]},
            {"type": "result", "structured": {"answer": answer, "resources": list(resources)},
             "context_tokens": 1000},
        ]
    return reply


def test_index_shows_ask_buttons_only_when_enabled(setup):
    _login(setup.client)
    html = setup.client.get("/").data.decode()
    assert 'data-key="1:assignment:2"' in html and 'id="tutor"' in html
    assert 'data-key="1:assignment:11"' not in html   # done: no button
    setup.fake.stop()
    html = setup.client.get("/").data.decode()
    assert "btn-ask\" data-key" not in html and 'id="tutor"' not in html


def test_ask_streams_answer_and_resumes_session(setup):
    _login(setup.client)
    setup.fake.reply = _reply(resources=[{"title": "Bitesize", "url": "https://bbc.co.uk/x"},
                                         {"title": "bad", "url": "javascript:alert(1)"}])
    r, events = _ask(setup.client, item_key="1:assignment:2", question="What does osmosis mean?")
    assert r.status_code == 200
    chat_id = events[0]["chat_id"]
    assert "".join(e.get("delta", "") for e in events) == "Try thinking about which way water moves."
    statuses = [e["status"] for e in events if "status" in e]
    assert "Reading “Osmosis notes”…" in statuses
    assert "Searching the web for “osmosis bitesize”…" in statuses
    assert events[-1] == {"done": True, "resources": [{"title": "Bitesize", "url": "https://bbc.co.uk/x"}]}

    run = [q for q in setup.fake.requests if q["method"] == "POST"][-1]["body"]
    assert run["profile"] == "homework-tutor" and run["model"] == "claude-sonnet-5-5"
    assert run["builtin_tools"] == [] and set(run["mcp_tools"]) == {"list_items", "read_item", "web_search", "fetch_url"}
    assert run["stream_fields"] == ["answer"] and run["session"] == {"new": True}
    assert chat_id in run["prompt"] and "Brief for Soon worksheet" in run["prompt"]
    assert "[1:page:osmosis] Osmosis notes" in run["prompt"] and "What does osmosis mean?" in run["prompt"]
    assert "homework-tutor" in setup.fake.profiles

    first_session = setup.fake.runs[run["run_id"]][0]["session_id"]
    r, events = _ask(setup.client, chat_id=chat_id, question="And then?")
    run2 = [q for q in setup.fake.requests if q["method"] == "POST"][-1]["body"]
    assert run2["session"] == {"resume": first_session}
    assert "Brief for" not in run2["prompt"] and "And then?" in run2["prompt"]
    assert events[-1]["done"]


def test_ask_requires_login_and_json(setup):
    r = setup.client.post("/api/ask", json={"item_key": "1:assignment:2", "question": "hi"})
    assert r.status_code == 401
    _login(setup.client)
    r = setup.client.post("/api/ask", data={"item_key": "1:assignment:2", "question": "hi"})
    assert r.status_code == 415


def test_ask_refuses_done_unknown_and_foreign_chats(setup):
    _login(setup.client)
    r, _ = _ask(setup.client, item_key="1:assignment:11", question="hi")
    assert r.status_code == 400
    r, _ = _ask(setup.client, item_key="9:assignment:999", question="hi")
    assert r.status_code == 404

    other = setup.tutor.chats.create("someone-else", "right", "1:assignment:2")
    r, _ = _ask(setup.client, chat_id=other.id, question="hi")   # not theirs, no item: refused
    assert r.status_code == 404
    r, events = _ask(setup.client, chat_id=other.id, item_key="1:assignment:2", question="hi")
    assert events[0]["chat_id"] != other.id                       # a fresh chat of their own


def test_ask_rate_limited(setup, monkeypatch):
    _login(setup.client)
    setup.tutor.limiter.per_hour = 1
    _ask(setup.client, item_key="1:assignment:2", question="one")
    _, events = _ask(setup.client, item_key="1:assignment:2", question="two")
    assert "a lot of questions" in events[-1]["error"]


def test_ask_broker_errors_are_friendly(setup):
    _login(setup.client)

    def busy(body):
        raise FakeHTTPError(429, "queue full")
    setup.fake.reply = busy
    _, events = _ask(setup.client, item_key="1:assignment:2", question="q")
    assert events[-1] == {"error": "The helper is busy right now. Try again in a minute."}

    setup.fake.reply = lambda body: [{"type": "error", "kind": "timeout", "detail": "x"}]
    _, events = _ask(setup.client, item_key="1:assignment:2", question="q2")
    assert "too long" in events[-1]["error"]

    _, events = _ask(setup.client, item_key="1:assignment:2", question="x" * 2000)
    assert "under 1000" in events[-1]["error"]


def test_context_reset_starts_fresh_session(setup):
    _login(setup.client)
    setup.fake.reply = lambda body: [{"type": "result", "structured": {"answer": "Hint.", "resources": []},
                                      "context_tokens": 999_999}]
    _, events = _ask(setup.client, item_key="1:assignment:2", question="q")
    chat_id = events[0]["chat_id"]
    _ask(setup.client, chat_id=chat_id, question="q2")
    run2 = [q for q in setup.fake.requests if q["method"] == "POST"][-1]["body"]
    assert run2["session"] == {"new": True} and "Brief for Soon worksheet" in run2["prompt"]


# -- MCP tools socket --------------------------------------------------------------------

class Rpc:
    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(path)
        self.reader = self.sock.makefile("rb")
        self.n = 0

    def call(self, method, params=None):
        self.n += 1
        self.sock.sendall(json.dumps({"jsonrpc": "2.0", "id": self.n, "method": method,
                                      "params": params or {}}).encode() + b"\n")
        return json.loads(self.reader.readline())

    def close(self):
        self.reader.close()
        self.sock.close()


def test_mcp_tools_over_socket(setup):
    t = setup.tutor
    t.start()
    path = os.path.join(t.broker_dir, "tools.sock")
    assert os.path.exists(path)
    app_module.cache.login("kid", "right")
    chat = t.open_chat("kid", "right", "1:assignment:2")

    rpc = Rpc(path)
    try:
        init = rpc.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
        assert init["result"]["serverInfo"]["name"] == "homework"
        rpc.sock.sendall(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        names = [x["name"] for x in rpc.call("tools/list")["result"]["tools"]]
        assert names == ["list_items", "read_item", "web_search", "fetch_url"]

        listed = rpc.call("tools/call", {"name": "list_items", "arguments": {"chat": chat.id}})["result"]
        assert not listed["isError"] and "[1:page:osmosis] Osmosis notes" in listed["content"][0]["text"]

        read = rpc.call("tools/call", {"name": "read_item",
                                       "arguments": {"chat": chat.id, "key": "1:page:osmosis"}})["result"]
        assert "Water moves" in read["content"][0]["text"]

        bad = rpc.call("tools/call", {"name": "read_item", "arguments": {"chat": "nope", "key": "1:page:osmosis"}})
        assert bad["result"]["isError"] and "chat id" in bad["result"]["content"][0]["text"]
        missing = rpc.call("tools/call", {"name": "read_item", "arguments": {"chat": chat.id, "key": "zzz"}})
        assert missing["result"]["isError"]
        assert rpc.call("nope")["error"]["code"] == -32601
    finally:
        rpc.close()


def test_mcp_web_tools_use_web_module(setup, monkeypatch):
    calls = []
    monkeypatch.setattr(web_tools, "web_search", lambda q: calls.append(q) or
                        [{"title": "Osmosis - BBC Bitesize", "url": "https://bbc.co.uk/o", "snippet": "Water..."}])
    monkeypatch.setattr(web_tools, "fetch_url", lambda u: (_ for _ in ()).throw(web_tools.WebError("private")))
    out = setup.tutor.tool_web_search("osmosis")
    assert "https://bbc.co.uk/o" in out and calls == ["osmosis"]
    with pytest.raises(tutor_module.ToolError):
        setup.tutor.tool_fetch_url("http://10.0.0.1/")


# -- web tools ------------------------------------------------------------------------------

DDG_HTML = """
<div class="results">
 <div class="result results_links result--ad"><div class="links_main">
  <a class="result__a" href="https://duckduckgo.com/y.js?ad=1">Buy stuff</a>
  <a class="result__snippet">An advert</a></div></div>
 <div class="result results_links"><div class="links_main">
  <h2 class="result__title"><a rel="nofollow" class="result__a"
    href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.bbc.co.uk%2Fbitesize%2Fosmosis&amp;rut=abc">Osmosis - <b>BBC</b> Bitesize</a></h2>
  <a class="result__snippet" href="x">Osmosis is the <b>diffusion</b> of water.</a></div></div>
 <div class="result results_links"><div class="links_main">
  <h2 class="result__title"><a class="result__a" href="https://www.khanacademy.org/osmosis">Khan</a></h2>
  <a class="result__snippet">Video</a></div></div>
</div>"""


def test_parse_ddg():
    results = web_tools.parse_ddg(DDG_HTML)
    assert results == [
        {"title": "Osmosis - BBC Bitesize", "url": "https://www.bbc.co.uk/bitesize/osmosis",
         "snippet": "Osmosis is the diffusion of water."},
        {"title": "Khan", "url": "https://www.khanacademy.org/osmosis", "snippet": "Video"},
    ]


@pytest.mark.parametrize("url", ["http://127.0.0.1/", "http://10.1.2.3/x", "http://192.168.0.5/",
                                 "http://169.254.169.254/latest", "http://[::1]/", "file:///etc/passwd",
                                 "ftp://example.com/", "http://localhost:5000/"])
def test_check_public_refuses_private(url):
    with pytest.raises(web_tools.WebError):
        web_tools.check_public(url)


def test_readable_url_google_docs():
    assert web_tools.readable_url("https://docs.google.com/document/d/AbC_1/edit?usp=sharing") == \
        "https://docs.google.com/document/d/AbC_1/export?format=txt"
    assert web_tools.readable_url("https://docs.google.com/presentation/d/X9/edit#slide=1") == \
        "https://docs.google.com/presentation/d/X9/export/txt"
    assert web_tools.readable_url("https://example.com/a") == "https://example.com/a"


class FakeResp:
    def __init__(self, status=200, headers=None, body=b""):
        self.status_code = status
        self.headers = headers or {}
        self.body = body
        self.encoding = "utf-8"
        self.is_redirect = status in (301, 302)

    def iter_content(self, n):
        yield self.body

    def close(self):
        pass


class FakeSession:
    def __init__(self, routes):
        self.routes = routes
        self.got = []

    def get(self, url, **kw):
        self.got.append(url)
        return self.routes[url]


def test_fetch_url_checks_every_redirect():
    checked = []

    def check(url):
        checked.append(url)
        if "10.0.0.1" in url:
            raise web_tools.WebError("private")

    s = FakeSession({"https://ok.example/": FakeResp(302, {"Location": "http://10.0.0.1/admin"})})
    with pytest.raises(web_tools.WebError):
        web_tools.fetch_url("https://ok.example/", session=s, check=check)
    assert checked == ["https://ok.example/", "http://10.0.0.1/admin"]
    assert s.got == ["https://ok.example/"]


def test_fetch_url_html_to_text():
    s = FakeSession({"https://ok.example/": FakeResp(200, {"Content-Type": "text/html"},
                                                     b"<html><script>x()</script><h1>Osmosis</h1><p>Water &amp; cells</p></html>")})
    text = web_tools.fetch_url("https://ok.example/", session=s, check=lambda u: None)
    assert text == "Osmosis\n\nWater & cells"


class SearchSession:
    def __init__(self, ddg_status=200, ddg_html=DDG_HTML):
        self.ddg_status, self.ddg_html = ddg_status, ddg_html

    def post(self, url, **kw):
        return SimpleNamespace(status_code=self.ddg_status, text=self.ddg_html)

    def get(self, url, **kw):
        assert url == web_tools.WIKIPEDIA_URL
        data = {"query": {"search": [{"title": "Osmosis", "snippet": '<span class="searchmatch">Osmosis</span> is'}]}}
        return SimpleNamespace(status_code=200, json=lambda: data)


def test_web_search_uses_ddg_then_falls_back_to_wikipedia(monkeypatch):
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    assert web_tools.web_search("osmosis", session=SearchSession())[0]["url"] == "https://www.bbc.co.uk/bitesize/osmosis"
    results = web_tools.web_search("osmosis", session=SearchSession(ddg_status=202))
    assert results[0]["title"] == "(Note)"
    assert results[1] == {"title": "Osmosis - Wikipedia", "url": "https://en.wikipedia.org/wiki/Osmosis",
                          "snippet": "Osmosis is"}
