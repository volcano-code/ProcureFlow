"""Streaming ingress budget is enforced before multipart parser accumulation."""
import asyncio
import httpx
from conftest import BUYER, request


def test_chunked_multipart_upload_stops_before_body_accumulation(system, monkeypatch):
    client, service, _ = system
    rid = request(client)["id"]
    sent = 0
    import starlette.formparsers as forms
    original, temporary_files = forms.SpooledTemporaryFile, []
    def retain_spool(*args, **kwargs):
        result = original(*args, **kwargs)
        temporary_files.append(result)
        return result
    monkeypatch.setattr(forms, "SpooledTemporaryFile", retain_spool)

    async def exercise():
        async def body():
            nonlocal sent
            yield b'--x\r\nContent-Disposition: form-data; name="file"; filename="large.csv"\r\nContent-Type: text/csv\r\n\r\n'
            for _ in range(100):
                sent += 1
                yield b'a' * 65536
            yield b'\r\n--x--\r\n'
        transport = httpx.ASGITransport(app=client.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as raw:
            return await raw.post(f"/api/v1/requests/{rid}/table-imports", headers={**BUYER,
                "Content-Type": "multipart/form-data; boundary=x"}, content=body())

    response = asyncio.run(exercise())
    assert response.status_code == 413, response.text
    assert sent < 100
    assert temporary_files and all(file.closed for file in temporary_files)
    assert not list(service.document_dir.iterdir())
