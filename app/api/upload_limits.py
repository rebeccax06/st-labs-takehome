"""Bound request bodies before multipart parsing, including chunked requests."""

from starlette.requests import Request

from app.api.errors import response
from app.ingestion.parsing import MAX_BYTES


class IngestBodyLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"] not in {"/ingest", "/ingest/raw"}:
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        media = headers.get(b"content-type", b"").split(b";", 1)[0].lower()
        # JSON envelopes can escape each source character. The pipeline separately
        # checks decoded content against 1 MiB; multipart gets bounded form overhead.
        limit = 6 * MAX_BYTES + 65536 if media == b"application/json" else (
            MAX_BYTES + 65536 if media == b"multipart/form-data" else MAX_BYTES)
        chunks, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > limit:
                result = response(Request(scope), 413, "limit_exceeded", "Upload exceeds the supported size (1 MiB of raw content).")
                return await result(scope, receive, send)
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)
        delivered = False
        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()
        await self.app(scope, replay, send)
