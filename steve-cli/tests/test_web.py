"""The web tools against real Chromium and a local site. The site is 127.0.0.1, which the tools must
refuse unless it is allowlisted, so most tests allowlist it and the others check the refusal."""

import asyncio
import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fake_upstream import Step

from steve_agent import web
from steve_agent.models.configs import SteveWebConfig
from steve_agent.web import HostPolicy, Refused, WebSession, browser_installed, make_web_tools

# Tests get a temporary HOME, and the browser is in the real one.
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(Path.home() / ".cache/ms-playwright"))

try:
    AVAILABLE = browser_installed(SteveWebConfig())
except Exception:
    AVAILABLE = False
needs_browser = pytest.mark.skipif(not AVAILABLE, reason="no browser: run steve-agent web setup")


def small_pdf(text):
    stream = f"BT /F1 18 Tf 40 700 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    start = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    return out + b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, start)


ARTICLE = "<p>" + "This is a long paragraph of real article text about herons and their habits. " * 8 + "</p>"


def html(title, body):
    return f"<html><head><title>{title}</title></head><body><article><h1>{title}</h1>{body}</article></body></html>"


class Site:
    def __init__(self):
        self.visits = []
        self.user_agents = set()
        self.pages = {
            "/": html("Home", ARTICLE + '<a href="/a">A</a> <a href="/b">B</a> <a href="/secret">S</a>'),
            "/a": html("Page A", ARTICLE + '<a href="/a/deep">deep</a>'),
            "/a/deep": html("Deep", ARTICLE + "DEEP_TEXT"),
            "/b": html("Page B", ARTICLE),
            "/secret": html("Secret", "ROBOTS_FORBID_THIS"),
            "/script": html("Scripted", '<div id="x"></div><script>document.getElementById("x").textContent = "'
                            + "RENDEREDBYSCRIPT and more words so that this counts as article text. " * 6 + '"</script>'),
        }
        self.robots = "User-agent: *\nDisallow: /secret\n"
        site = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                site.visits.append(self.path)
                site.user_agents.add(self.headers.get("User-Agent"))
                if self.path == "/robots.txt":
                    return self.send(200, site.robots.encode(), "text/plain")
                if self.path == "/brief.pdf":
                    return self.send(200, small_pdf("The launch code is 7731."), "application/pdf")
                if self.path == "/slow":
                    threading.Event().wait(30)
                if self.path == "/missing":
                    return self.send(404, b"nope", "text/html")
                if self.path.startswith("/to/"):
                    self.send_response(302)
                    self.send_header("Location", "http://" + self.path[4:])
                    self.end_headers()
                    return None
                page = site.pages.get(self.path)
                return self.send(200, page.encode(), "text/html") if page else self.send(404, b"nope", "text/html")

            def send(self, status, body, kind):
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def site():
    site = Site()
    yield site
    site.server.shutdown()


class MemoryFiles:
    def __init__(self):
        self.files = {}

    async def awrite(self, path, content):
        self.files[path] = content
        return type("Result", (), {"error": None, "path": path})()


def tools(files=None, **settings):
    settings.setdefault("allowed_hosts", ["127.0.0.1"])
    fetch, crawl = make_web_tools(SteveWebConfig(**settings), files or MemoryFiles(), "/artifacts/crawl")
    return fetch, crawl


def fetch(url, **settings):
    return asyncio.run(tools(**settings)[0].ainvoke({"url": url}))


def crawl(files=None, **arguments):
    settings = {key: arguments.pop(key) for key in ("allowed_hosts", "obey_robots", "search_url") if key in arguments}
    return asyncio.run(tools(files, **settings)[1].ainvoke(arguments))


# The address policy, which needs no browser


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.0.0.1", "172.16.0.1", "192.168.1.1", "169.254.169.254", "100.64.0.1", "0.0.0.0", "::1", "fe80::1",
     "fc00::1", "::ffff:127.0.0.1", "::ffff:10.0.0.1", "224.0.0.1"],
)
def test_private_addresses_are_refused(address):
    assert not HostPolicy([]).address_allowed(address)


def test_public_addresses_are_allowed():
    assert HostPolicy([]).address_allowed("1.1.1.1") and HostPolicy([]).address_allowed("2606:4700:4700::1111")


def test_a_network_can_be_allowlisted():
    policy = HostPolicy(["192.168.0.0/16"])
    assert policy.address_allowed("192.168.1.5") and not policy.address_allowed("10.0.0.1")


@pytest.mark.parametrize(("host", "entry", "allowed"), [
    ("localhost", "localhost", True),
    ("LOCALHOST", "localhost", True),
    ("localhost", "other.test", False),
    ("a.localhost", "*.localhost", True),
    ("evillocalhost", "*.localhost", False),
])
def test_names_on_the_allowlist(host, entry, allowed):
    async def check():
        await HostPolicy([entry]).resolve(host, 80)

    if allowed:
        asyncio.run(check())
    else:
        with pytest.raises(Refused):
            asyncio.run(check())


def test_a_name_with_one_private_address_is_refused(monkeypatch):
    """A public name pointing at a private address is how this kind of block usually gets around."""

    async def getaddrinfo(self, host, port, **_):
        return [(2, 1, 6, "", ("1.1.1.1", port)), (2, 1, 6, "", ("10.0.0.5", port))]

    monkeypatch.setattr("asyncio.base_events.BaseEventLoop.getaddrinfo", getaddrinfo)
    with pytest.raises(Refused, match="private"):
        asyncio.run(HostPolicy([]).resolve("rebind.test", 80))


# web_fetch


@needs_browser
def test_fetch_returns_main_text_title_and_links(site):
    result = fetch(site.url + "/")
    assert "herons" in result and "# Home" in result
    assert f"{site.url}/a" in result
    assert result.startswith("Untrusted content from")


@needs_browser
def test_fetch_renders_scripts(site):
    assert "RENDEREDBYSCRIPT" in fetch(site.url + "/script")


@needs_browser
def test_fetch_reads_a_pdf(site):
    assert "7731" in fetch(site.url + "/brief.pdf")


@needs_browser
def test_fetch_says_when_a_page_is_missing(site):
    assert "HTTP 404" in fetch(site.url + "/missing")


@needs_browser
def test_it_says_what_it_is(site):
    fetch(site.url + "/")
    assert site.user_agents == {"SteveAgent/0.1"}


@needs_browser
@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/", "javascript:alert(1)", "chrome://version"])
def test_only_web_addresses_can_be_read(url):
    assert "Only http and https" in fetch(url)


# Private addresses, through the real browser


@needs_browser
def test_this_machine_is_refused(site):
    result = fetch(site.url + "/", allowed_hosts=[])
    assert "private address" in result
    assert "herons" not in result
    assert site.visits == []


@needs_browser
def test_a_redirect_to_this_machine_is_refused(site):
    target = Site()
    # The redirect comes from an allowlisted name, and points to an address that is not allowlisted.
    result = fetch(f"http://localhost:{site.server.server_address[1]}/to/{target.url.removeprefix('http://')}/", allowed_hosts=["localhost"])
    assert "private address" in result, result
    assert target.visits == []


@needs_browser
def test_what_a_page_loads_is_refused_too(site):
    target = Site()
    site.pages["/loads"] = html(
        "Loads",
        ARTICLE
        + f'<img src="{target.url}/img"><script src="{target.url}/js"></script><iframe src="{target.url}/frame"></iframe>'
        + f'<script>fetch("{target.url}/fetch"); new WebSocket("{target.url.replace("http", "ws")}/ws");</script>',
    )
    result = fetch(f"http://localhost:{site.server.server_address[1]}/loads", allowed_hosts=["localhost"])
    assert "herons" in result, "the page itself still reads"
    assert target.visits == []


# robots.txt


@needs_browser
def test_robots_is_obeyed_by_default(site):
    result = fetch(site.url + "/secret")
    assert "robots.txt" in result and "ROBOTS_FORBID_THIS" not in result
    assert "/secret" not in site.visits


@needs_browser
def test_robots_can_be_turned_off(site):
    assert "Secret" in fetch(site.url + "/secret", obey_robots=False)


# web_crawl


@needs_browser
def test_crawl_saves_pages_and_returns_an_index(site):
    files = MemoryFiles()
    result = crawl(files, start_url=site.url + "/", max_depth=1, max_pages=10)
    assert "Saved 3 pages" in result
    assert len(files.files) == 3
    assert all(path.startswith("/artifacts/crawl/") for path in files.files)
    assert "herons" not in result, "the index stays small, and the pages stay in the files"
    for path in files.files:
        assert path in result


@needs_browser
def test_crawl_skips_what_robots_forbids(site):
    files = MemoryFiles()
    result = crawl(files, start_url=site.url + "/", max_depth=1)
    assert "/secret" not in site.visits
    assert "ROBOTS_FORBID_THIS" not in json.dumps(files.files)
    assert "robots.txt" in result


@needs_browser
def test_crawl_respects_depth_and_page_limits(site):
    files = MemoryFiles()
    crawl(files, start_url=site.url + "/", max_depth=0)
    assert len(files.files) == 1
    files = MemoryFiles()
    result = crawl(files, start_url=site.url + "/", max_depth=2, max_pages=2)
    assert len(files.files) == 2 and "Stopped at 2 pages" in result


@needs_browser
def test_crawl_follows_include_and_exclude(site):
    files = MemoryFiles()
    crawl(files, start_url=site.url + "/", max_depth=1, include=["*/b"])
    assert "/a" not in site.visits and "/b" in site.visits
    site.visits.clear()
    crawl(MemoryFiles(), start_url=site.url + "/", max_depth=1, exclude=["*/b"])
    assert "/a" in site.visits and "/b" not in site.visits


@needs_browser
def test_crawl_stays_on_the_site(site):
    other = Site()
    site.pages["/"] = html("Home", ARTICLE + f'<a href="{other.url}/">Away</a> <a href="/b">B</a>')
    crawl(MemoryFiles(), start_url=f"http://localhost:{site.server.server_address[1]}/", max_depth=1,
          allowed_hosts=["localhost", "127.0.0.1"])
    assert other.visits == []


@needs_browser
def test_crawl_visits_each_address_once(site):
    site.pages["/"] = html("Home", ARTICLE + '<a href="/b">1</a> <a href="/b#part">2</a> <a href="/b">3</a>')
    crawl(MemoryFiles(), start_url=site.url + "/", max_depth=1)
    assert site.visits.count("/b") == 1


@needs_browser
def test_crawl_with_a_query_reads_what_the_search_page_finds(site):
    other = Site()
    site.pages["/find"] = html("Results", ARTICLE + f'<a href="{other.url}/a">Result</a>')
    site.robots = ""
    files = MemoryFiles()
    search = f"http://localhost:{site.server.server_address[1]}/find?q={{query}}"
    site.pages["/find?q=big%20birds"] = site.pages["/find"]
    result = crawl(files, query="big birds", search_url=search, allowed_hosts=["localhost", "127.0.0.1"])
    assert "Saved" in result and any("Page A" in text for text in files.files.values())


@needs_browser
def test_a_search_page_that_finds_nothing_says_so(site):
    """As when a search engine answers with a page asking if you are a robot."""
    site.pages["/find"] = html("Unusual traffic", ARTICLE)
    site.robots = ""
    result = crawl(query="birds", search_url=site.url + "/find")
    assert "refused the browser as a bot" in result


@needs_browser
def test_awkward_links_are_handled(site):
    """Search pages use SVG links, whose href is not a string. This took a whole run down."""
    site.pages["/"] = html(
        "Home",
        ARTICLE
        + '<svg><a href="/b"><text>svg link</text></a></svg>'
        + '<svg xmlns:xlink="http://www.w3.org/1999/xlink"><a xlink:href="/a"><text>old svg link</text></a></svg>'
        + '<a href="http://[bad">broken</a> <a href="">empty</a> <a href="mailto:a@b.test">mail</a>',
    )
    files = MemoryFiles()
    result = crawl(files, start_url=site.url + "/", max_depth=1)
    assert "Saved 2 pages" in result, result
    assert "/b" in site.visits


@needs_browser
def test_a_page_that_navigates_away_does_not_end_the_crawl(site):
    site.pages["/"] = html("Home", ARTICLE + '<a href="/jumpy">J</a> <a href="/b">B</a>')
    site.pages["/jumpy"] = html("Jumpy", ARTICLE + '<script>setInterval(() => { location.reload(); }, 20)</script>')
    files = MemoryFiles()
    result = crawl(files, start_url=site.url + "/", max_depth=1)
    assert "Saved" in result
    assert any("Page B" in text for text in files.files.values())


@needs_browser
def test_a_page_that_breaks_reading_does_not_end_the_crawl(site, monkeypatch):
    real = web._main_text

    def explode(text, url):
        if url.endswith("/a"):
            raise RuntimeError("extraction broke")
        return real(text, url)

    monkeypatch.setattr(web, "_main_text", explode)
    files = MemoryFiles()
    result = crawl(files, start_url=site.url + "/", max_depth=1)
    assert "extraction broke" in result, "the agent is told what it could not read"
    assert any("Page B" in text for text in files.files.values())
    assert "extraction broke" in fetch(site.url + "/a")


def test_a_query_needs_a_search_page():
    assert "No search page" in crawl(query="birds")


@needs_browser
def test_a_query_obeys_the_search_pages_robots(site):
    site.pages["/find"] = html("Results", ARTICLE)
    site.robots = "User-agent: *\nDisallow: /find\n"
    result = crawl(query="birds", search_url=site.url + "/find")
    assert "Saved 0 pages" in result and "robots.txt" in result


@needs_browser
def test_stopping_a_crawl_closes_the_browser(site):
    marker = "--user-data-dir"

    def browsers():
        return subprocess.run(["pgrep", "-fc", f"playwright.*{marker}"], capture_output=True, text=True).stdout.strip()

    before = browsers()

    async def scenario():
        task = asyncio.create_task(tools()[1].ainvoke({"start_url": site.url + "/slow"}))
        for _ in range(100):
            await asyncio.sleep(0.1)
            if "/slow" in site.visits:
                break
        assert "/slow" in site.visits
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert browsers() == before


@pytest.mark.parametrize(
    ("choice", "expected"),
    [
        ("chrome", {"channel": "chrome"}),
        ("msedge", {"channel": "msedge"}),
        ("/usr/bin/chromium-browser", {"executable_path": "/usr/bin/chromium-browser"}),
        (None, {}),
        ("", {}),
    ],
)
def test_the_browser_setting_is_a_channel_or_a_path(choice, expected):
    assert web._browser_options(choice) == expected


def test_a_windows_path_counts_as_a_path():
    assert "executable_path" in web._browser_options("C:\\Program Files\\Brave\\brave.exe")


def test_a_missing_browser_says_how_to_fix_it(site):
    result = fetch(site.url + "/", browser="/nonexistent/chromium")
    assert "steve-agent web setup" in result


@needs_browser
def test_a_browser_of_your_own_can_be_chosen(site):
    """Any Chromium-based browser by path. Here the one Playwright installed, found the same way a person would."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        path = playwright.chromium.executable_path
    if not os.path.exists(path):
        pytest.skip("only the headless shell is installed")
    assert "herons" in fetch(site.url + "/", browser=path)


# Through the real agent


@needs_browser
def test_crawled_pages_can_be_read_with_the_file_tools(start_server, upstream, site, tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    # A real server's settings come from its config, so this one is allowlisted the same way.
    server = start_server(workspace=str(workspace), web={"allowed_hosts": ["127.0.0.1"]})
    call = ("web_crawl", json.dumps({"start_url": site.url + "/", "max_depth": 0}))
    upstream.steps.extend([Step(text="", tools=[call]), Step(text="Crawled.")])
    response = server.client.responses.create(input="go")
    tool = next(item for item in response.output if item.type == "mcp_call")
    assert "Saved 1 pages" in tool.output, tool.output
    path = next(word.rstrip(":") for word in tool.output.split() if word.startswith("/artifacts/crawl/") and word.endswith(":"))
    assert not list(workspace.iterdir()), "crawls do not land in the project"

    upstream.steps.extend([Step(text="", tools=[("read_file", json.dumps({"file_path": path}))]), Step(text="Read.")])
    after = server.client.responses.create(input="again", previous_response_id=response.id)
    read = next(item for item in after.output if item.type == "mcp_call")
    assert "herons" in read.output


def offered(upstream):
    return {tool.get("name") for tool in upstream.requests[0]["body"].get("tools", [])}


def test_planning_can_read_the_web(start_server, upstream):
    server = start_server()
    upstream.steps.append(Step(text="A plan."))
    server.client.responses.create(input="go", extra_body={"mode": "planning"})
    assert {"web_fetch", "web_crawl"} <= offered(upstream)


def test_manual_asks_before_reading_the_web(start_server, upstream, site):
    from test_modes import Conversation

    server = start_server()
    upstream.steps.extend(
        [Step(text="", tools=[("web_fetch", json.dumps({"url": site.url + "/"}))]), Step(text="Understood.")]
    )
    run = Conversation(server, lambda event: {"approve": False}, input="go", mode="manual")
    assert run.questions[0]["tool"] == "web_fetch"
    assert site.visits == []


def test_the_web_tools_can_be_turned_off(start_server, upstream):
    server = start_server(web={"enabled": False})
    upstream.steps.append(Step(text="Hi."))
    server.client.responses.create(input="go")
    assert not {"web_fetch", "web_crawl"} & offered(upstream)
    assert "web_fetch" not in upstream.body_text()
