"""Run only against the native Compose web gate, not the direct-API dev server."""
import json
from urllib.parse import urlparse
from workbench_e2e import page, prepare, URL
from playwright.sync_api import expect


def test_proxy_keeps_auth_and_scope_boundaries(page):
    page.goto(URL)
    result = page.evaluate("""async () => {
      const noAuth = await fetch('/backend/api/v1/requests');
      const invalid = await fetch('/backend/api/v1/requests', {headers:{Authorization:'Bearer invalid'}});
      const rpc = await fetch('/backend/api/method/frappe.auth.get_logged_user');
      const docs = await fetch('/backend/docs');
      return {noAuth:noAuth.status, invalid:invalid.status, rpc:rpc.status, docs:docs.status,
              cache:noAuth.headers.get('cache-control')};
    }""")
    assert result == {"noAuth": 401, "invalid": 401, "rpc": 404, "docs": 404, "cache": "no-store"}
    prepare(page)
    expect(page.get_by_test_id('quote-row')).to_have_count(3)


def test_browser_uses_only_same_origin_business_api(page):
    requests = []
    page.on("request", lambda request: requests.append(request.url))
    prepare(page)
    business = [url for url in requests if '/api/v1/' in url]
    assert len(business) >= 8
    origin = urlparse(URL)
    assert all(urlparse(url).netloc == origin.netloc and urlparse(url).path.startswith('/backend/api/v1/') for url in business)


def test_audit_sse_reaches_browser_through_native_proxy(page):
    prepare(page)
    result = page.evaluate("""async () => {
      const headers = {Authorization:'Bearer demo-buyer'};
      const requests = await (await fetch('/backend/api/v1/requests', {headers})).json();
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 8000);
      let reader;
      try {
        const r = await fetch('/backend/api/v1/requests/' + requests[0].id + '/events/stream',
                              {headers, signal:controller.signal});
        reader = r.body.getReader();
        let text = '';
        while (!text.includes('data:')) {
          const chunk = await reader.read();
          if (chunk.done) break;
          text += new TextDecoder().decode(chunk.value);
        }
        return {status:r.status, type:r.headers.get('content-type'), text};
      } finally {clearTimeout(timer); if(reader) await reader.cancel(); controller.abort();}
    }""")
    assert result['status'] == 200 and result['type'].startswith('text/event-stream')
    assert 'data:' in result['text'] and 'id:' in result['text']
    assert 'demo-buyer' not in result['text']
