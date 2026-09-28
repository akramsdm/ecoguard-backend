from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from .cache import hub
from .models import User
from .security import current_user

router = APIRouter(prefix='/stream', tags=['Realtime updates'])


# EventSource cannot send an Authorization header, so the session cookie authenticates
# this stream. Payloads are metadata-only, but they still carry internal record ids and
# the timing of case activity, which is not for anonymous callers.
@router.get('')
async def stream(request: Request, _user: User = Depends(current_user)):
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