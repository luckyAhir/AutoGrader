"""Deterministic generation of test inputs from a declarative specification.

The instructor describes the input format once in the rules::

    "input_spec": {
      "variables": [
        {"name": "n", "type": "int", "min": 0, "max": 100000, "role": "size"},
        {"name": "values", "type": "int[]", "length": "n",
         "min": -2147483648, "max": 2147483647}
      ],
      "layout": [["n"], ["values"]]
    }

A variable's ``type`` is ``int``, ``float`` or ``str``, optionally followed by ``[]`` (a list: give its
``length``) or ``[][]`` (a matrix: give ``rows`` and ``cols``; each row is printed on its own line).
Common names (``integer``, ``double``, ``string``, ``matrix`` ...) are accepted. ``length``, ``rows`` and
``cols`` are numbers or the names of earlier variables.

The specification is used to check that a test case has the right shape (``parse``, ``valid``), and to produce
inputs of a given size for the complexity estimator (``for_size``). Edge cases are not generated here: an LLM
writes them (see ``autograder.testgen``).

Every random choice uses a ``random.Random`` seeded from the rules, so the same rules always produce the same
inputs.

Problems that do not fit the declarative format can name a Python generator file in the rules
(``"generator": "gen.py"``) that defines ``for_size(n, rng) -> str``.
"""

from __future__ import annotations

import importlib.util
import random
import string
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .util import INT32_MAX, INT32_MIN, stable_seed

WHITESPACE_VARIANTS = {
    "tabs": "values separated by tab characters",
    "multi_space": "several spaces between values and at the line ends",
    "one_per_line": "every value on its own line",
    "crlf": "Windows line endings (\\r\\n)",
    "no_trailing_newline": "no newline after the last line",
    "leading_blank_line": "an empty first line",
}


class InputSpecError(ValueError):
    pass


@dataclass
class Case:
    category: str
    text: str
    values: Optional[Dict[str, Any]] = None
    variant: str = "normal"
    note: str = ""


@dataclass
class Var:
    name: str
    type: str                    # canonical: int | float | str, plus [] (list) or [][] (matrix)
    min: Any
    max: Any
    length: Any = None           # lists: number or name of an earlier variable
    role: Optional[str] = None
    charset: str = string.ascii_lowercase
    order: Optional[str] = None  # int lists/matrices: "asc" or "desc" ("sorted": true means "asc")
    distinct: bool = False       # int lists/matrices: no value repeats
    rows: Any = None             # matrices: number or name of an earlier variable
    cols: Any = None

    @property
    def base(self) -> str:
        return self.type.split("[")[0]

    @property
    def dims(self) -> int:
        return self.type.count("[]")


BASE_ALIASES = {
    "int": "int", "integer": "int", "long": "int", "longlong": "int", "int32": "int", "int64": "int",
    "uint": "int", "unsigned": "int", "short": "int", "size_t": "int",
    "float": "float", "double": "float", "real": "float", "decimal": "float",
    "str": "str", "string": "str", "char": "char", "character": "char", "text": "str", "word": "str",
}
CONTAINER_ALIASES = {"array": "int[]", "list": "int[]", "vector": "int[]", "matrix": "int[][]", "grid": "int[][]",
                     "2darray": "int[][]", "array2d": "int[][]"}
TYPE_HELP = ('use "int", "float", "str" or "char", optionally followed by [] for a list or [][] for a matrix '
             '(e.g. "int[]", "float[][]"); "char[]" is a text, "char[][]" a grid of characters')
MAX_ELEMENTS = 2_000_000


class _TooBig(Exception):
    """A generated input would hold too many values; the case is skipped."""


def canonical_type(raw: Any) -> str:
    t = str(raw if raw is not None else "int").strip().lower().replace(" ", "").replace("_", "")
    if t in CONTAINER_ALIASES:
        return CONTAINER_ALIASES[t]
    dims = 0
    while t.endswith("[]") and dims < 3:
        t, dims = t[:-2], dims + 1
    base = BASE_ALIASES.get(t)
    if base is None or dims > 2:
        raise InputSpecError(f"unsupported type {raw!r}; {TYPE_HELP}")
    if base == "char" and dims == 1:  # a character array is a text such as "hello"
        return "str"
    return base + "[]" * dims


class InputGenerator:
    def __init__(self, spec: Optional[Dict[str, Any]], seed_material: str,
                 plugin_path: Optional[Path] = None):
        self.seed_material = seed_material
        self.plugin = _load_plugin(plugin_path) if plugin_path else None
        self.vars: List[Var] = []
        self.layout: List[List[str]] = []
        self.size_var: Optional[str] = None
        self.dim_names: set = set()
        if spec:
            self._parse_spec(spec)
        elif not self.plugin:
            raise InputSpecError("an input_spec or a generator is required")

    # ------------------------------------------------------------ spec
    def _parse_spec(self, spec: Dict[str, Any]) -> None:
        raw_vars = spec.get("variables")
        if not isinstance(raw_vars, list) or not raw_vars:
            raise InputSpecError("input_spec.variables must be a non-empty list")
        names = set()
        for rv in raw_vars:
            if not isinstance(rv, dict) or not isinstance(rv.get("name"), str) or not rv["name"]:
                raise InputSpecError('every entry of input_spec.variables needs a "name"')
            where = f"variable {rv['name']!r}"
            if rv["name"] in names:
                raise InputSpecError(f"{where} is declared twice")
            try:
                vtype = canonical_type(rv.get("type", "int"))
                base = vtype.split("[")[0]
                if base == "str":  # "min_length"/"max_length" are accepted as well
                    rv = dict(rv, min=rv.get("min", rv.get("min_length", 1)), max=rv.get("max", rv.get("max_length", 20)))
                if base == "char":  # one character per value
                    rv = dict(rv, min=1, max=1)
                if base == "float":
                    lo, hi = float(rv.get("min", -1e6)), float(rv.get("max", 1e6))
                else:
                    lo, hi = int(rv.get("min", INT32_MIN)), int(rv.get("max", INT32_MAX))
            except InputSpecError as exc:
                raise InputSpecError(f"{where}: {exc}") from exc
            except (TypeError, ValueError) as exc:
                raise InputSpecError(f"{where}: \"min\" and \"max\" must be numbers") from exc
            if lo > hi:
                raise InputSpecError(f"{where}: min > max")
            order = rv.get("sorted")
            order = "asc" if order is True else (order or None)
            if order not in (None, "asc", "desc"):
                raise InputSpecError(f"{where}: \"sorted\" must be true, \"asc\" or \"desc\"")
            letters = string.ascii_letters if str(rv.get("type", "")).strip().lower().startswith("char") \
                else string.ascii_lowercase
            v = Var(rv["name"], vtype, lo, hi, None, rv.get("role"), str(rv.get("charset") or letters),
                    order, bool(rv.get("distinct", False)))
            dims = v.dims
            if dims == 1:
                v.length = _first(rv, "length", "size", "count", "n")
                if v.length is None:
                    raise InputSpecError(f'{where} is a list: give its "length" (a number or an earlier variable)')
                refs = [("length", v.length)]
            elif dims == 2:
                v.rows = _first(rv, "rows", "height", "length")
                v.cols = _first(rv, "cols", "columns", "width")
                if v.rows is None or v.cols is None:
                    raise InputSpecError(f'{where} is a matrix: give its "rows" and "cols" (numbers or earlier variables)')
                refs = [("rows", v.rows), ("cols", v.cols)]
            elif v.base == "str":  # a text may have a fixed length, e.g. the value of an earlier variable
                v.length = _first(rv, "length", "size")
                refs = [("length", v.length)] if v.length is not None else []
            else:
                refs = []
            for key, ref in refs:
                if isinstance(ref, str):
                    if ref not in names:
                        raise InputSpecError(f"{where}: \"{key}\" names {ref!r}, which must be declared before it")
                    self.dim_names.add(ref)
                elif isinstance(ref, bool) or not isinstance(ref, int) or ref < 0:
                    raise InputSpecError(f"{where}: \"{key}\" must be a whole number or the name of a variable")
            names.add(v.name)
            self.vars.append(v)
        for v in self.vars:
            if v.role == "size":
                self.size_var = v.name
        if self.size_var is None:
            for v in self.vars:
                ref = v.length if v.dims == 1 else v.rows if v.dims == 2 else None
                if isinstance(ref, str):
                    self.size_var = ref
                    break
        layout = spec.get("layout") or [[v.name] for v in self.vars]
        if not isinstance(layout, list) or any(not isinstance(line, list) for line in layout):
            raise InputSpecError('"layout" must be a list of lines, each a list of variable names, '
                                 'e.g. [["n", "m"], ["A"]]')
        self.layout = layout
        declared = {v.name for v in self.vars}
        for line in self.layout:
            for name in line:
                if name not in declared:
                    raise InputSpecError(f"layout mentions unknown variable {name!r}")

    def var(self, name: str) -> Var:
        return next(v for v in self.vars if v.name == name)

    @property
    def scalar_vars(self) -> List[Var]:
        """Integer variables that carry values (not the ones that set a list's or matrix's size)."""
        return [v for v in self.vars if v.type == "int" and v.name not in self.dim_names and v.name != self.size_var]

    @property
    def array_vars(self) -> List[Var]:
        """Integer lists and matrices."""
        return [v for v in self.vars if v.base == "int" and v.dims >= 1]

    @property
    def vector_vars(self) -> List[Var]:
        return [v for v in self.vars if v.base == "int" and v.dims == 1]

    def size_of(self, values: Optional[Dict[str, Any]]) -> Optional[int]:
        if values and self.size_var and self.size_var in values:
            return int(values[self.size_var])
        return None

    # ------------------------------------------------------------ rendering
    @staticmethod
    def _fmt(x: Any) -> str:
        return format(x, ".10g") if isinstance(x, float) else str(x)

    def render(self, values: Dict[str, Any], variant: str = "normal") -> str:
        lines: List[List[str]] = []
        for line_vars in self.layout:
            if len(line_vars) == 1 and self.var(line_vars[0]).dims == 2:  # a matrix: one row per line
                grid = self.var(line_vars[0]).base == "char"  # a character grid: "#.#", not "# . #"
                for row in values[line_vars[0]]:
                    lines.append(["".join(row)] if grid else [self._fmt(x) for x in row])
                continue
            toks: List[str] = []
            for name in line_vars:
                v = values[name]
                toks.extend(self._fmt(x) for x in _flatten(v)) if isinstance(v, list) else toks.append(self._fmt(v))
            lines.append(toks)
        if variant == "tabs":
            return "\n".join("\t".join(t) for t in lines) + "\n"
        if variant == "multi_space":
            return "\n".join("  " + "   ".join(t) + "  " for t in lines) + "\n"
        if variant == "one_per_line":
            flat = [tok for t in lines for tok in t]
            return "\n".join(flat) + "\n"
        if variant == "crlf":
            return "\r\n".join(" ".join(t) for t in lines) + "\r\n"
        if variant == "no_trailing_newline":
            return "\n".join(" ".join(t) for t in lines)
        if variant == "leading_blank_line":
            return "\n" + "\n".join(" ".join(t) for t in lines) + "\n"
        return "\n".join(" ".join(t) for t in lines) + "\n"

    def describe(self, values: Optional[Dict[str, Any]]) -> str:
        if not values:
            return ""
        parts = []
        for v in self.vars:
            val = values.get(v.name)
            if isinstance(val, list):
                flat = _flatten(val)
                shown = ", ".join(self._fmt(x) for x in flat[:6]) + (", ..." if len(flat) > 6 else "")
                parts.append(f"{v.name}=[{shown}]")
            else:
                parts.append(f"{v.name}={val}")
        return ", ".join(parts)

    @staticmethod
    def _is_num(x: Any) -> bool:
        return isinstance(x, (int, float)) and not isinstance(x, bool)

    def _ok_value(self, v: Var, x: Any) -> bool:
        if v.base == "int":
            return isinstance(x, int) and not isinstance(x, bool) and v.min <= x <= v.max
        if v.base == "float":
            return self._is_num(x) and v.min <= x <= v.max
        if v.base == "char":
            return isinstance(x, str) and len(x) == 1 and x in v.charset
        return isinstance(x, str) and v.min <= len(x) <= v.max and set(x) <= set(v.charset)

    def valid(self, values: Dict[str, Any]) -> bool:
        for v in self.vars:
            val = values.get(v.name)
            if v.dims == 0:
                if not self._ok_value(v, val):
                    return False
                if v.base == "str" and v.length is not None and len(val) != self._dim(v.length, values):
                    return False
                continue
            if not isinstance(val, list):
                return False
            if v.dims == 1:
                if len(val) != self._dim(v.length, values) or any(not self._ok_value(v, x) for x in val):
                    return False
                flat = val
            else:
                rows, cols = self._dim(v.rows, values), self._dim(v.cols, values)
                if len(val) != rows or any(not isinstance(r, list) or len(r) != cols for r in val):
                    return False
                flat = _flatten(val)
                if any(not self._ok_value(v, x) for x in flat):
                    return False
            if v.distinct and len(set(flat)) != len(flat):
                return False
            if v.order and flat != sorted(flat, reverse=v.order == "desc"):
                return False
        return True

    def parse(self, text: str) -> Optional[Dict[str, Any]]:
        """Read an input back into values (tokens in layout order); None if it breaks the spec."""
        if not self.vars:
            return None
        tokens = text.split()
        pos = 0
        values: Dict[str, Any] = {}

        def conv(v: Var, tok: str) -> Any:
            return int(tok) if v.base == "int" else float(tok) if v.base == "float" else tok

        try:
            for name in (n for line in self.layout for n in line):
                v = self.var(name)
                if v.dims == 0:
                    if pos >= len(tokens):
                        return None
                    values[name] = conv(v, tokens[pos])
                    pos += 1
                    continue
                if v.dims == 1:
                    rows, cols = self._dim(v.length, values), 1
                else:
                    rows, cols = self._dim(v.rows, values), self._dim(v.cols, values)
                if v.base == "char" and v.dims == 2:  # each row is one token of `cols` characters
                    if rows < 0 or cols < 0 or rows * cols > MAX_ELEMENTS or pos + rows > len(tokens):
                        return None
                    values[name] = [list(t) for t in tokens[pos:pos + rows]]
                    pos += rows
                    continue
                count = rows * cols
                if rows < 0 or cols < 0 or count > MAX_ELEMENTS or pos + count > len(tokens):
                    return None
                flat = [conv(v, t) for t in tokens[pos:pos + count]]
                pos += count
                values[name] = flat if v.dims == 1 else [flat[i * cols:(i + 1) * cols] for i in range(rows)]
        except (ValueError, KeyError):
            return None
        return values if pos == len(tokens) and self.valid(values) else None

    @staticmethod
    def _dim(ref: Any, values: Dict[str, Any]) -> int:
        if isinstance(ref, str):
            return int(values[ref])
        return int(ref if ref is not None else 0)

    # ------------------------------------------------------------ building blocks
    def _rng(self, *salt: Any) -> random.Random:
        return random.Random(stable_seed(self.seed_material, *salt))

    @staticmethod
    def _clamp(v: Var, x: Any) -> Any:
        return max(v.min, min(v.max, x))

    def _typical(self, v: Var, rng: random.Random) -> Any:
        if v.base == "char":
            return rng.choice(v.charset)
        if v.base == "str":
            n = rng.randint(max(int(v.min), 1), max(min(int(v.max), 10), max(int(v.min), 1)))
            return "".join(rng.choice(v.charset) for _ in range(n))
        if v.base == "float":
            lo, hi = max(v.min, -1000.0), min(v.max, 1000.0)
            if lo > hi:
                lo, hi = v.min, v.max
            return round(rng.uniform(lo, hi), 2)
        if v.name == self.size_var or v.name in self.dim_names:
            lo = max(v.min, 2) if v.max >= 2 else v.min
            return rng.randint(lo, max(lo, min(v.max, 8)))
        lo, hi = max(v.min, -1000), min(v.max, 1000)
        if lo > hi:
            lo, hi = v.min, v.max
        return rng.randint(lo, hi)

    def _build(self, rng: random.Random, overrides: Optional[Dict[str, Any]] = None,
               element: Optional[Callable[[Var, int, random.Random], int]] = None,
               arrange: Optional[Callable[[List[int]], List[int]]] = None) -> Dict[str, Any]:
        overrides = overrides or {}
        values: Dict[str, Any] = {}
        for v in self.vars:
            if v.name in overrides:
                values[v.name] = overrides[v.name]
            elif v.dims == 0:
                if v.base == "str" and v.length is not None:  # fixed length, e.g. as many characters as n says
                    values[v.name] = "".join(rng.choice(v.charset) for _ in range(max(0, self._dim(v.length, values))))
                else:
                    values[v.name] = self._typical(v, rng)
            else:
                if v.dims == 1:
                    rows, cols = self._dim(v.length, values), 1
                else:
                    rows, cols = self._dim(v.rows, values), self._dim(v.cols, values)
                count = max(0, rows) * max(0, cols)
                if count > MAX_ELEMENTS:
                    raise _TooBig()
                if v.base == "int" and element:
                    flat = [self._clamp(v, element(v, i, rng)) for i in range(count)]
                else:
                    flat = [self._typical(v, rng) for _ in range(count)]
                if v.base == "int":
                    flat = self._conform(v, arrange(flat) if arrange else flat)
                values[v.name] = flat if v.dims == 1 else [flat[i * cols:(i + 1) * cols] for i in range(max(0, rows))]
        return values

    @staticmethod
    def _conform(v: Var, arr: List[int]) -> List[int]:
        """Apply the array's constraints: replace repeats by the nearest unused value, then sort."""
        if v.distinct:
            used: set = set()
            out: List[int] = []
            for x in arr:
                if x in used:
                    nearest = (c for d in range(1, len(arr) + 1) for c in (x + d, x - d)
                               if v.min <= c <= v.max and c not in used)
                    x = next(nearest, x)  # range too small: left as is, valid() drops the case
                used.add(x)
                out.append(x)
            arr = out
        return sorted(arr, reverse=v.order == "desc") if v.order else arr

    # ------------------------------------------------------------ complexity
    def for_size(self, n: int) -> Case:
        """An input whose 'size' is n.

        With a size variable (e.g. the array length) that variable is set to n.
        Without one, every scalar integer is set to n: for problems such as
        "sum 1..n" or "is n prime" the running time grows with the value
        itself, so the value is the natural size.
        """
        rng = self._rng("size", n)
        if self.plugin is not None and hasattr(self.plugin, "for_size"):
            return Case("size", self.plugin.for_size(n, rng))
        if self.size_var:
            v = self.var(self.size_var)
            overrides = {v.name: self._clamp(v, n)}
            # The other dimensions (e.g. the columns of an n x m matrix) stay the same at every size, so only
            # one dimension grows and the measured growth is clean.
            for name in sorted(self.dim_names - {v.name}):
                other = self.var(name)
                overrides[name] = self._typical(other, self._rng("size-dim", name))
            values = self._build(rng, overrides, element=lambda var, i, r: r.randint(var.min, var.max))
        else:
            values = self._build(rng, {v.name: self._clamp(v, n) for v in self.scalar_vars})
        return Case("size", self.render(values), values, note=f"n={n}")


def _first(raw: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if raw.get(key) is not None:
            return raw[key]
    return None


def _flatten(values: List[Any]) -> List[Any]:
    out: List[Any] = []
    for x in values:
        out.extend(_flatten(x)) if isinstance(x, list) else out.append(x)
    return out


def _load_plugin(path: Path):
    spec = importlib.util.spec_from_file_location(f"autograder_generator_{abs(hash(str(path)))}", str(path))
    if spec is None or spec.loader is None:
        raise InputSpecError(f"cannot load generator {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "for_size"):
        raise InputSpecError(f"generator {path} must define for_size(n, rng)")
    return module
