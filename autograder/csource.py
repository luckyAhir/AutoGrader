"""A lightweight model of a C translation unit.

The source-only analyzers (structure, modularity, readability, originality) need
tokens, comments, function boundaries and declared names. A full C parser would
need preprocessed input and system headers, and would reject code that does not
compile. A tolerant tokenizer is enough for metrics, and it keeps working on
broken submissions, which still get structure and readability feedback.

Metrics follow the usual definitions (as in lizard / McCabe):
* cyclomatic complexity = 1 + number of ``if for while case && || ?``
* NLOC = number of distinct lines that hold code tokens of the function
* nesting = maximum brace depth inside the function body
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from .util import normalize_newlines

C_KEYWORDS = frozenset("""
auto break case char const continue default do double else enum extern float for goto if
inline int long register restrict return short signed sizeof static struct switch typedef
union unsigned void volatile while _Alignas _Alignof _Atomic _Bool _Complex _Generic
_Imaginary _Noreturn _Static_assert _Thread_local
""".split())

TYPE_SPECIFIERS = frozenset("""
void char short int long float double signed unsigned _Bool bool _Complex size_t ssize_t
ptrdiff_t int8_t int16_t int32_t int64_t uint8_t uint16_t uint32_t uint64_t intptr_t
uintptr_t intmax_t uintmax_t FILE
""".split())
TYPE_QUALIFIERS = frozenset("const volatile static extern register auto inline restrict _Atomic _Thread_local".split())
TAG_KEYWORDS = frozenset(("struct", "union", "enum"))
DECISION_KEYWORDS = frozenset(("if", "for", "while", "case"))
DECISION_OPERATORS = frozenset(("&&", "||", "?"))

READER_CALLS = frozenset("scanf fscanf getchar fgets gets getline getc fgetc read fread getdelim".split())
WRITER_CALLS = frozenset("printf fprintf puts putchar fputs fwrite write putc fputc".split())

# Library names kept verbatim when normalising code for similarity: two programs
# that both call printf are not "similar" in any interesting way, but keeping the
# names makes the token stream more specific than a bare "ID".
STD_NAMES = frozenset("""
printf scanf fprintf fscanf sprintf snprintf sscanf puts gets fgets fputs putchar getchar
malloc calloc realloc free memcpy memmove memset memcmp strlen strcpy strncpy strcat strncat
strcmp strncmp strchr strrchr strstr strtok qsort bsearch abs labs llabs atoi atol atoll
strtol strtoll strtoul strtod exit main sqrt pow fabs floor ceil log exp sin cos fopen fclose
fread fwrite assert isdigit isalpha isspace isupper islower toupper tolower stdin stdout stderr
NULL EOF INT_MAX INT_MIN LLONG_MAX LLONG_MIN size_t bool true false
""".split())

_PP_RE = re.compile(r"#(?:\\\n|/\*[\s\S]*?\*/|[^\n])*")
_TOKEN_RE = re.compile(r"""
    (?P<ws>[ \t\r\f\v]+)
  | (?P<nl>\n)
  | (?P<lcomment>//(?:\\\n|[^\n])*)
  | (?P<bcomment>/\*[\s\S]*?(?:\*/|\Z))
  | (?P<str>(?:u8|[uUL])?"(?:\\[\s\S]|[^"\\\n])*"?)
  | (?P<chr>[uUL]?'(?:\\[\s\S]|[^'\\\n])*'?)
  | (?P<num>\.?[0-9](?:[eEpP][+-]|[0-9A-Za-z_.])*)
  | (?P<id>[A-Za-z_$][A-Za-z0-9_$]*)
  | (?P<op>>>=|<<=|\.\.\.|->|\+\+|--|<<|>>|<=|>=|==|!=|&&|\|\||[-+*/%&^|]=|\#\#|\S)
""", re.X)


@dataclass(frozen=True)
class Token:
    kind: str  # id | kw | num | str | chr | op
    text: str
    line: int


@dataclass
class Comment:
    text: str
    line: int
    end_line: int
    block: bool

    @property
    def body(self) -> str:
        t = self.text
        t = t[2:-2] if self.block and t.endswith("*/") else t[2:]
        return "\n".join(l.strip().lstrip("*").strip() for l in t.split("\n")).strip()


@dataclass
class Directive:
    text: str
    line: int


@dataclass
class Function:
    name: str
    return_type: str
    params: List[Optional[str]]
    start_line: int
    end_line: int
    body_start: int  # token index of '{'
    body_end: int    # token index of '}'
    nloc: int
    cyclomatic: int
    max_nesting: int
    statements: int
    calls: List[str]


@dataclass
class Declaration:
    name: str
    kind: str  # local | param | global | function
    line: int
    function: Optional[str] = None
    is_loop_counter: bool = False
    is_const: bool = False


@dataclass
class CSource:
    text: str
    lines: List[str]
    tokens: List[Token]
    comments: List[Comment]
    directives: List[Directive]
    partner: List[int]
    functions: List[Function] = field(default_factory=list)
    prototypes: List[str] = field(default_factory=list)
    declarations: List[Declaration] = field(default_factory=list)
    typedef_names: Set[str] = field(default_factory=set)

    # ------------------------------------------------------------ queries
    def function(self, name: str) -> Optional[Function]:
        return next((f for f in self.functions if f.name == name), None)

    @property
    def includes(self) -> List[str]:
        out = []
        for d in self.directives:
            m = re.match(r"#\s*include\s*[<\"]([^>\"]+)[>\"]", d.text)
            if m:
                out.append(m.group(1))
        return out

    @property
    def globals(self) -> List[Declaration]:
        return [d for d in self.declarations if d.kind == "global"]

    def code_lines(self) -> Set[int]:
        return {t.line for t in self.tokens}

    def comment_lines(self) -> Set[int]:
        lines: Set[int] = set()
        for c in self.comments:
            lines.update(range(c.line, c.end_line + 1))
        return lines

    def called_names(self) -> Set[str]:
        names = set()
        for i, t in enumerate(self.tokens[:-1]):
            if t.kind == "id" and self.tokens[i + 1].text == "(":
                names.add(t.text)
        return names

    def body_tokens(self, fn: Function) -> List[Token]:
        return self.tokens[fn.body_start + 1: fn.body_end]


# ---------------------------------------------------------------- tokenizer
def _tokenize(text: str) -> Tuple[List[Token], List[Comment], List[Directive]]:
    tokens: List[Token] = []
    comments: List[Comment] = []
    directives: List[Directive] = []
    pos, line, at_line_start = 0, 1, True
    n = len(text)
    while pos < n:
        if at_line_start and text[pos] == "#":
            m = _PP_RE.match(text, pos)
            directives.append(Directive(m.group().strip(), line))
            line += m.group().count("\n")
            pos = m.end()
            continue
        m = _TOKEN_RE.match(text, pos)
        kind, s = m.lastgroup, m.group()
        if kind == "nl":
            line += 1
            at_line_start = True
        elif kind == "ws":
            pass
        elif kind in ("lcomment", "bcomment"):
            comments.append(Comment(s, line, line + s.count("\n"), kind == "bcomment"))
            line += s.count("\n")
        else:
            if kind == "id" and s in C_KEYWORDS:
                kind = "kw"
            tokens.append(Token(kind, s, line))
            line += s.count("\n")
            at_line_start = False
        pos = m.end()
    return tokens, comments, directives


def _match_brackets(tokens: List[Token]) -> List[int]:
    partner = [-1] * len(tokens)
    pairs = {")": "(", "]": "[", "}": "{"}
    stack: List[int] = []
    for i, t in enumerate(tokens):
        if t.kind != "op":
            continue
        if t.text in "([{":
            stack.append(i)
        elif t.text in pairs:
            # tolerate unbalanced code: pop until the matching opener
            while stack and tokens[stack[-1]].text != pairs[t.text]:
                stack.pop()
            if stack:
                j = stack.pop()
                partner[i], partner[j] = j, i
    return partner


# ---------------------------------------------------------------- parsing
def parse(text: str) -> CSource:
    text = normalize_newlines(text)
    tokens, comments, directives = _tokenize(text)
    src = CSource(text=text, lines=text.split("\n"), tokens=tokens, comments=comments,
                  directives=directives, partner=_match_brackets(tokens))
    _scan_top_level(src)
    return src


def _is_type_start(src: CSource, tok: Token) -> bool:
    return (tok.text in TYPE_SPECIFIERS or tok.text in TYPE_QUALIFIERS
            or tok.text in TAG_KEYWORDS or tok.text in src.typedef_names)


def _parse_declaration(src: CSource, p: int, end: int):
    """Parse ``specifiers declarator[, declarator...]`` starting at token p.

    Returns ``([(name, line)], is_const, next_index)``; an empty list means the
    tokens at p do not start a declaration.
    """
    toks, partner = src.tokens, src.partner
    k, saw_type, is_const = p, False, False
    while k < end:
        t = toks[k]
        if t.text in TAG_KEYWORDS:
            saw_type = True
            k += 1
            if k < end and toks[k].kind == "id":
                k += 1
            if k < end and toks[k].text == "{":
                k = partner[k] + 1 if partner[k] > k else end
            continue
        if t.text in TYPE_SPECIFIERS or t.text in src.typedef_names:
            saw_type = True
            k += 1
            continue
        if t.text in TYPE_QUALIFIERS:
            is_const = is_const or t.text == "const"
            k += 1
            continue
        break
    if not saw_type:
        return [], False, p
    names: List[Tuple[str, int]] = []
    while k < end:
        while k < end and toks[k].text in ("*", "const", "volatile", "restrict"):
            k += 1
        if k >= end:
            break
        t = toks[k]
        if t.text == "(" and partner[k] > k:  # (*fp)(...) function pointer
            inner = [x for x in toks[k + 1:partner[k]] if x.kind == "id"]
            if not inner:
                break
            names.append((inner[0].text, inner[0].line))
            k = partner[k] + 1
        elif t.kind == "id":
            names.append((t.text, t.line))
            k += 1
        else:
            break
        while k < end and toks[k].text not in (",", ";", ")"):
            if toks[k].text in ("(", "[", "{") and partner[k] > k:
                k = partner[k] + 1
            else:
                k += 1
        if k < end and toks[k].text == ",":
            k += 1
            continue
        break
    return names, is_const, k


def _param_names(src: CSource, start: int, end: int) -> List[Optional[str]]:
    toks, partner = src.tokens, src.partner
    chunks: List[List[int]] = [[]]
    k = start
    while k < end:
        if toks[k].text == ",":
            chunks.append([])
        else:
            chunks[-1].append(k)
            if toks[k].text in ("(", "[") and partner[k] > k:
                chunks[-1].extend(range(k + 1, partner[k] + 1))
                k = partner[k]
        k += 1
    names: List[Optional[str]] = []
    for chunk in chunks:
        texts = [toks[i].text for i in chunk]
        if not chunk or texts == ["void"] or texts == ["..."]:
            continue
        name = None
        # function pointer parameter: int (*cmp)(const void *, const void *)
        for idx in chunk:
            if toks[idx].text == "(" and idx + 1 < len(toks) and toks[idx + 1].text == "*":
                inner = [toks[j] for j in range(idx + 1, partner[idx]) if toks[j].kind == "id"]
                name = inner[0].text if inner else None
                break
        if name is None:
            depth = 0
            for idx in chunk:
                t = toks[idx]
                if t.text in ("[", "("):
                    depth += 1
                elif t.text in ("]", ")"):
                    depth -= 1
                elif depth == 0 and t.kind == "id" and t.text not in TYPE_SPECIFIERS \
                        and t.text not in src.typedef_names:
                    name = t.text
        names.append(name)
    return names


def _scan_top_level(src: CSource) -> None:
    toks, partner = src.tokens, src.partner
    i, stmt_start, n = 0, 0, len(toks)
    while i < n:
        t = toks[i]
        if t.text == "{" and t.kind == "op":
            close = partner[i]
            is_func = (i > 0 and toks[i - 1].text == ")" and partner[i - 1] > 0
                       and toks[partner[i - 1] - 1].kind == "id")
            if is_func and close > i:
                _add_function(src, stmt_start, partner[i - 1] - 1, i, close)
                i = close + 1
                stmt_start = i
                continue
            if close < 0:
                break
            i = close + 1  # struct body / initializer: skip, statement continues
            continue
        if t.text == ";":
            _top_statement(src, stmt_start, i)
            stmt_start = i + 1
        i += 1


def _top_statement(src: CSource, start: int, end: int) -> None:
    toks, partner = src.tokens, src.partner
    if start >= end:
        return
    texts = [t.text for t in toks[start:end]]
    if texts[0] == "typedef":
        k, last = start, None
        while k < end:
            if toks[k].text in ("{", "(", "[") and partner[k] > k:
                if toks[k].text == "(":  # typedef int (*cmp_fn)(...)
                    inner = [x for x in toks[k + 1:partner[k]] if x.kind == "id"]
                    if inner and toks[k + 1].text == "*":
                        last = inner[0].text
                k = partner[k] + 1
                continue
            if toks[k].kind == "id":
                last = toks[k].text
            k += 1
        if last:
            src.typedef_names.add(last)
        return
    if "extern" in texts:
        return
    k = start
    while k < end:
        if toks[k].text == "=":
            break
        if toks[k].text == "(":
            if k > start and toks[k - 1].kind == "id":
                src.prototypes.append(toks[k - 1].text)
                return
            break
        if toks[k].text in ("[", "{") and partner[k] > k:
            k = partner[k] + 1
            continue
        k += 1
    names, is_const, _ = _parse_declaration(src, start, end)
    for name, line in names:
        src.declarations.append(Declaration(name, "global", line, None, False, is_const))


def _add_function(src: CSource, stmt_start: int, name_idx: int, open_idx: int, close_idx: int) -> None:
    toks, partner = src.tokens, src.partner
    name = toks[name_idx].text
    ret = " ".join(t.text for t in toks[stmt_start:name_idx])
    params = _param_names(src, name_idx + 2, partner[name_idx + 1])
    body = toks[open_idx + 1:close_idx]

    cyclomatic = 1 + sum(1 for t in body if (t.kind == "kw" and t.text in DECISION_KEYWORDS)
                         or (t.kind == "op" and t.text in DECISION_OPERATORS))
    depth = max_depth = 0
    for t in body:
        if t.text == "{":
            depth += 1
            max_depth = max(max_depth, depth)
        elif t.text == "}":
            depth -= 1
    statements, calls = 0, []
    k = open_idx + 1
    while k < close_idx:
        t = toks[k]
        if t.text == "for" and toks[k + 1].text == "(" and partner[k + 1] > k:
            statements += 1
            k = partner[k + 1] + 1
            continue
        if t.text == ";":
            statements += 1
        if t.kind == "id" and toks[k + 1].text == "(" and toks[k - 1].text not in (".", "->"):
            if t.text not in calls:
                calls.append(t.text)
        k += 1
    nloc = len({t.line for t in toks[stmt_start:close_idx + 1]})
    fn = Function(name, ret, params, toks[name_idx].line, toks[close_idx].line, open_idx,
                  close_idx, nloc, cyclomatic, max_depth, statements, calls)
    src.functions.append(fn)

    if name != "main":
        src.declarations.append(Declaration(name, "function", toks[name_idx].line, None))
    for p in params:
        if p:
            src.declarations.append(Declaration(p, "param", toks[name_idx].line, name))
    _scan_locals(src, fn)


def _scan_locals(src: CSource, fn: Function) -> None:
    toks, partner = src.tokens, src.partner
    loop_counters: Set[str] = set()
    found: List[Declaration] = []
    k, at_start = fn.body_start + 1, True
    while k < fn.body_end:
        t = toks[k]
        if t.text == "for" and toks[k + 1].text == "(" and partner[k + 1] > k:
            close = partner[k + 1]
            init_end = next((j for j in range(k + 2, close) if toks[j].text == ";"), close)
            names, is_const, _ = _parse_declaration(src, k + 2, init_end)
            for name, line in names:
                found.append(Declaration(name, "local", line, fn.name, True, is_const))
                loop_counters.add(name)
            for j in range(k + 2, init_end - 1):
                if toks[j].kind == "id" and toks[j + 1].text == "=":
                    loop_counters.add(toks[j].text)
            k += 2
            at_start = False  # the for-init declaration was handled above
            continue
        if at_start and _is_type_start(src, t):
            names, is_const, nk = _parse_declaration(src, k, fn.body_end)
            if names:
                for name, line in names:
                    found.append(Declaration(name, "local", line, fn.name, False, is_const))
                k = max(nk, k + 1)
                at_start = False
                continue
        at_start = t.text in ("{", "}", ";")
        k += 1
    for d in found:
        d.is_loop_counter = d.is_loop_counter or d.name in loop_counters
    src.declarations.extend(found)


# ---------------------------------------------------------------- normalisation
def normalized_tokens(src: CSource) -> List[Tuple[str, int]]:
    """Token stream with identifiers/literals abstracted (MOSS/JPlag style).

    Renaming variables, editing comments, or reformatting whitespace leaves this
    stream unchanged, which is what makes copy detection robust to cosmetic edits.
    """
    out: List[Tuple[str, int]] = []
    for t in src.tokens:
        if t.kind == "id":
            sym = t.text if t.text in STD_NAMES else "ID"
        elif t.kind == "num":
            sym = "NUM"
        elif t.kind == "str":
            sym = "STR"
        elif t.kind == "chr":
            sym = "CHR"
        else:
            sym = t.text
        out.append((sym, t.line))
    return out


def find_calls(src: CSource, names: Set[str]) -> Dict[str, List[int]]:
    """Lines on which each of ``names`` is called."""
    found: Dict[str, List[int]] = {}
    for i, t in enumerate(src.tokens[:-1]):
        if t.kind == "id" and t.text in names and src.tokens[i + 1].text == "(":
            found.setdefault(t.text, []).append(t.line)
    return found
