import asyncio

import pytest
import uvicorn

from backend import server


@pytest.mark.parametrize("reload", [False, True])
def test_server_uses_selector_loop_with_and_without_reload(reload):
    # Build the loop the way Uvicorn does, so a wrong "module:attr" value fails here.
    config = uvicorn.Config("backend.main:app", loop=server.LOOP, reload=reload)
    loop = config.get_loop_factory()()
    try:
        assert isinstance(loop, asyncio.SelectorEventLoop)
    finally:
        loop.close()
