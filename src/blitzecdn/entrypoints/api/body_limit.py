"""Bound request bodies before FastAPI buffers or parses them."""

from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class RequestBodyLimitMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        detail = f"Request body exceeds {self.max_bytes} bytes"
        for name, value in scope["headers"]:
            if name == b"content-length":
                try:
                    length = int(value)
                except ValueError:
                    length = -1
                if length < 0:
                    await JSONResponse(
                        {"detail": "Invalid Content-Length"}, status_code=400
                    )(scope, receive, send)
                    return
                if length > self.max_bytes:
                    await JSONResponse({"detail": detail}, status_code=413)(
                        scope, receive, send
                    )
                    return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    # Raised inside the application's exception boundary. FastAPI
                    # preserves HTTPException while parsing; multipart parsing
                    # closes any temporary files as the exception unwinds.
                    raise HTTPException(status_code=413, detail=detail)
            return message

        await self.app(scope, limited_receive, send)
