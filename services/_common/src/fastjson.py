import orjson
from typing import Any, Callable, IO, Optional

def load(fp: IO[bytes]) -> Any:
  """
  Replacement for json.load().
  Expects a binary file opened with 'rb'.
  """
  return orjson.loads(fp.read())

def loads(s: bytes) -> Any:
  """
  Replacement for json.loads().
  Expects a bytes object.
  """
  return orjson.loads(s)

def dump(obj: Any, fp: IO[bytes], *, indent: Optional[int] = None, default: Optional[Callable[[Any], Any]] = None) -> None:
  """
  Replacement for json.dump().
  Expects a binary file opened with 'wb'.
  Supports indent=2 (only 2 spaces, per orjson) and default (called for
  values orjson doesn't natively serialize - same contract as stdlib's
  json.dump(default=...), e.g. default=str).
  """
  option = orjson.OPT_INDENT_2 if indent is not None else None
  fp.write(orjson.dumps(obj, default=default, option=option))

def dumps(obj: Any, *, indent: Optional[int] = None, default: Optional[Callable[[Any], Any]] = None) -> bytes:
  """
  Replacement for json.dumps().
  Returns a bytes object (not str - callers that need a str, e.g. for
  Path.write_text(), must .decode() the result themselves).
  Supports indent=2 (only 2 spaces) and default, same as dump() above.
  """
  option = orjson.OPT_INDENT_2 if indent is not None else None
  return orjson.dumps(obj, default=default, option=option)
