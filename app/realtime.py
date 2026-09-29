import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from .cache import hub
from .config import get_settings
from .db import SessionLocal
from .models import LoginSession, User
from .security import token_hash

router = APIRouter(prefix='/stream', tags=['Realtime updates'])


# EventSource cannot send an Authorization header, so the session cookie authenticates
# this stream. Payloads are metadata-only, but they still carry internal record ids and
# the timing of case activity, which is not for anonymous callers.
#
# The response never ends, so this route deliberately does NOT depend on current_user /
# get_db: FastAPI holds yield-dependencies until the response completes, which would park
# one pool connection in an open transaction for the entire lifetime of every connected
# client. A handful of staff tabs then exhaust the QueuePool (5 + 10 overflow) and every
# other request starts failing after a 30 s pool timeout. This helper reads the session
# synchronously and closes the connection before streaming begins.
async def _authenticate(request: Request) -> None:
    token = request.cookies.get(get_settings().session_cookie, '')
    with SessionLocal() as db:
        session = db.get(LoginSession, token_hash(token)) if token else None
        if not session or session.expires_at <= time.time():
            raise HTTPException(401, 'Please sign in again.')
        user = db.get(User, session.user_id)
        if not user or not user.active:
            raise HTTPException(401, 'Account is unavailable.')
    request.state.session = session


@router.get('')
async def stream(request: Request):
    await _authenticate(request)

    async def events():
        try:
            async for chunk in hub.subscribe():
                if await request.is_disconnected():
                    break
                yield chunk
        except Exception:
            pass

    return StreamingResponse(
        events(),
        media_type='text/event-stream',
        headers={
            'Cache-Control': 'no-cache, no-transform',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no',
        },
    )