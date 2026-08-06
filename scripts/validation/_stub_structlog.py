import types, sys

structlog = types.ModuleType("structlog")

class _Logger:
    def __init__(self, bound=None):
        self._bound = bound or {}

    def bind(self, **kw):
        merged = {**self._bound, **kw}
        return _Logger(merged)

    def _emit(self, level, msg, **kw):
        pass  # silence during validation runs; real behavior not under test here

    def info(self, msg, **kw): self._emit("info", msg, **kw)
    def warning(self, msg, **kw): self._emit("warning", msg, **kw)
    def debug(self, msg, **kw): self._emit("debug", msg, **kw)
    def error(self, msg, **kw): self._emit("error", msg, **kw)


def get_logger():
    return _Logger()


structlog.get_logger = get_logger
sys.modules["structlog"] = structlog
