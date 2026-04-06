# Lessons Learned

_Updated after every correction or mistake._

## From News Bot (imported)

### Windows compatibility
- ALWAYS set `encoding="utf-8"` on file handlers and `errors="replace"` on console handlers
- Always mock aiohttp.ClientSession at module level in tests
- Use `StrEnum`, `datetime.UTC`, `collections.abc.Callable`
- pathlib.Path partout

### Async patterns
- asyncio.gather with return_exceptions=True for independent tasks
- Wrap background tasks in _safe_task with auto-restart + backoff
- Never await a non-async method
- Requeue items in except block after flush failure

### Trading logic
- Never resolve paper trades using signal direction — use actual price
- Model both entry and exit fees
- Every resource acquired must have a corresponding release
- Validate prices are in valid range (0, 1)
