"""OpenFOAM 字典文件(dictionary)的解析与序列化。

核心思路(这是整个 GUI 的基础)
------------------------------------------------------------------
GUI 不重新"生成"字典, 而是采用 **解析 -> 修改 -> 序列化** 的方式:

1. 读取案例里已有的字典文件, 解析成一棵 :class:`FoamDict` 树;
2. GUI 只修改它关心的条目(例如 ``endTime``、某个 patch 的边界条件);
3. 再把这棵树原样写回文件。

这样做的好处是: 用户手写的、GUI 还没支持的任何条目都不会丢失,
因此可以放心地用它处理各种不同的案例。

支持的值类型
------------------------------------------------------------------
* ``str``          : 原子(atom), 例如 ``uniform``、``1e-05``、``$internalField``
* :class:`FoamList`: ``( ... )`` 列表, 元素可以是原子/列表/字典
* :class:`Compound`: 由多个部分组成的值, 例如 ``uniform (0 0 0)``、
                      ``nonuniform List<scalar> 100 (...)``
* :class:`Dimensioned`: ``[0 1 -1 0 0 0 0] 1e-05`` 这类带量纲的值
* :class:`FoamDict`: ``{ ... }`` 子字典
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Iterator

__all__ = [
    "FoamDict",
    "FoamList",
    "Compound",
    "Dimensioned",
    "ParseError",
    "parse_dict",
    "tokenize",
    "format_dict",
    "format_value",
    "dump",
    "load",
    "atom",
    "atoms",
    "get_atom",
    "get_dict",
    "get_list",
    "make_uniform",
    "uniform_parts",
    "as_float",
    "as_int",
    "clone_value",
]


class ParseError(Exception):
    """字典解析失败。"""


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
class FoamDict:
    """有序字典: OpenFOAM 字典的书写顺序会被保留。"""

    __slots__ = ("items",)

    def __init__(self, items: Iterable[tuple[str, Any]] | None = None):
        self.items: list[tuple[str, Any]] = list(items or [])

    # -- 基本字典接口 -------------------------------------------------------
    def keys(self) -> list[str]:
        return [k for k, _ in self.items]

    def __iter__(self) -> Iterator[str]:
        return iter(self.keys())

    def __len__(self) -> int:
        return len(self.items)

    def __contains__(self, key: str) -> bool:
        return any(k == key for k, _ in self.items)

    def __getitem__(self, key: str) -> Any:
        for k, v in self.items:
            if k == key:
                return v
        raise KeyError(key)

    def __setitem__(self, key: str, value: Any) -> None:
        self.set(key, value)

    def __delitem__(self, key: str) -> None:
        self.items = [(k, v) for k, v in self.items if k != key]

    def get(self, key: str, default: Any = None) -> Any:
        for k, v in self.items:
            if k == key:
                return v
        return default

    def set(self, key: str, value: Any) -> None:
        """就地修改(保持原有位置), 不存在则追加到末尾。"""
        for i, (k, _) in enumerate(self.items):
            if k == key:
                self.items[i] = (key, value)
                return
        self.items.append((key, value))

    def move_to_front(self, key: str) -> None:
        """把某个条目挪到最前面(OpenFOAM 习惯把 solver 写在 controlDict 开头)。"""
        for i, (k, v) in enumerate(self.items):
            if k == key:
                if i:
                    self.items.pop(i)
                    self.items.insert(0, (key, v))
                return

    def pop(self, key: str, default: Any = None) -> Any:
        v = self.get(key, _MISSING)
        if v is _MISSING:
            return default
        del self[key]
        return v

    def copy(self) -> "FoamDict":
        return FoamDict((k, clone_value(v)) for k, v in self.items)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"FoamDict({self.items!r})"


class FoamList(list):
    """``( ... )`` 列表。"""


class Compound(list):
    """由多个 token 组成的值, 例如 ``uniform (0 0 0)``。"""


class Directive:
    """OpenFOAM 的预处理指令行, 例如 ``#includeEtc "caseDicts/setConstraintTypes"``。

    整行原样保留: 指令后面的条目不能被吞进它的"值"里(否则写回时会写坏文件),
    重复出现多次的指令也必须按原顺序保留。
    """

    __slots__ = ("text",)

    def __init__(self, text: str):
        self.text = text

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"Directive({self.text!r})"


class Dimensioned:
    """带量纲的值。

    OpenFOAM 里有两种写法, 必须原样保留(顺序反了虽然多数场合也能读, 但没必要冒险):

    * 量纲在前: ``dimensions [0 2 -1 0 0 0 0];``、``nu [0 2 -1 0 0 0 0] 1e-05;``
    * 值在前:   ``nu 1e-05 [m^2/s];``(OpenFOAM 10 之后支持的"单位"写法,
                ``rho 1 [kg/m^3];`` 同理)
    """

    __slots__ = ("dims", "value", "dims_first")

    def __init__(self, dims: list[str], value: Any, dims_first: bool = True):
        self.dims = list(dims)
        self.value = value
        self.dims_first = dims_first

    def __repr__(self) -> str:  # pragma: no cover
        return f"Dimensioned({self.dims!r}, {self.value!r})"


_MISSING = object()


def clone_value(v: Any) -> Any:
    """对值做浅拷贝(足够用于 GUI 编辑)。"""
    if isinstance(v, FoamDict):
        return v.copy()
    if isinstance(v, Compound):
        return Compound(clone_value(x) for x in v)
    if isinstance(v, FoamList):
        return FoamList(clone_value(x) for x in v)
    if isinstance(v, Dimensioned):
        return Dimensioned(v.dims, clone_value(v.value))
    return v


# ---------------------------------------------------------------------------
# 词法分析
# ---------------------------------------------------------------------------
_TOKEN_RE = re.compile(
    r"""
      (?P<block>/\*.*?\*/)          # 块注释(含文件头)
    | (?P<line>//[^\n]*)            # 行注释
    | (?P<string>"(?:[^"\\]|\\.)*") # 双引号字符串
    | (?P<punct>[(){}\[\];])        # 标点
    | (?P<word>[^\s(){}\[\];]+)     # 单词/数字
    """,
    re.VERBOSE | re.DOTALL,
)


class _Token:
    __slots__ = ("kind", "text")

    def __init__(self, kind: str, text: str):
        self.kind = kind
        self.text = text

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{self.kind}:{self.text}>"


def tokenize(text: str, start: int = 0) -> list[_Token]:
    """把文本切成 token(去掉注释)。

    特别注意: OpenFOAM 允许"函数式"名字, 例如 ``div(phi,U)``、``grad(U)``、
    ``div((nuEff*dev2(T(grad(U)))))``。这类写法中间不能有空格, 且不能是纯数字
    (纯数字加括号是 ``nonuniform ... 100(...)`` 这种计数写法)。这里把它
    整体当作一个 token, 才能原样保留。
    """
    tokens: list[_Token] = []
    pos = start
    n = len(text)
    while pos < n:
        m = _TOKEN_RE.match(text, pos)
        if not m:
            # 理论上不会发生: word 规则可以匹配任意非空白字符
            pos += 1
            continue
        pos = m.end()
        kind = m.lastgroup
        if kind in ("block", "line"):
            continue
        tok = m.group()
        if kind == "word" and tok.startswith("#"):
            # 预处理/函数指令(#includeEtc/#ifeq/#calc/#neg/#codeStream ...):
            # 吃到行尾, 但要
            #   * 跳过字符串里的括号(例如 #calc "sqrt(x)" 里的 ')');
            #   * 遇到"不属于自己的"右括号就停 —— 例如
            #     ``internalField uniform (#neg $UMean 0 0);`` 里指令在列表内部,
            #     把 ')' 吞掉会让列表配不上对;
            #   * 末尾的分号不算指令的一部分(``wheelSpeed #calc "...";`` 是指令当值)。
            i = pos
            depth = 0
            in_str = False
            while i < n and text[i] != "\n":
                c = text[i]
                if in_str:
                    if c == "\\":
                        i += 2
                        continue
                    if c == '"':
                        in_str = False
                elif c == '"':
                    in_str = True
                elif c == "(":
                    depth += 1
                elif c == ")":
                    if depth == 0:
                        break
                    depth -= 1
                i += 1
            end = i
            j = end
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j < n and text[j] == "{":
                # #codeStream 这类后面跟代码块的: 把块也一起吃掉
                depth2 = 0
                k = j
                while k < n:
                    if text[k] == "{":
                        depth2 += 1
                    elif text[k] == "}":
                        depth2 -= 1
                        if depth2 == 0:
                            k += 1
                            break
                    k += 1
                end = k
            raw = text[m.start():end].rstrip()
            if raw.endswith(";") and not raw.endswith(";"):
                pass
            raw = raw[:-1].rstrip() if raw.endswith(";") else raw
            tokens.append(_Token("word", raw))
            pos = end
            continue
        if (
            kind == "word"
            and pos < n
            and text[pos] == "("
            and not tok.lstrip("+-").isdigit()
        ):
            depth = 0
            i = pos
            while i < n:
                c = text[i]
                if c == "(":
                    depth += 1
                elif c == ")":
                    depth -= 1
                    if depth == 0:
                        i += 1
                        break
                i += 1
            tok = text[m.start() : i]
            pos = i
        tokens.append(_Token(kind or "word", tok))
    return tokens


class _Parser:
    def __init__(self, tokens: list[_Token], pos: int = 0):
        self.tokens = tokens
        self.pos = pos

    def peek(self) -> _Token | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def next(self) -> _Token:
        t = self.peek()
        if t is None:
            raise ParseError("字典意外结束")
        self.pos += 1
        return t

    def expect(self, text: str) -> None:
        t = self.next()
        if t.text != text:
            raise ParseError(f"期望 {text!r}, 实际得到 {t.text!r}")

    # -- 语法 ---------------------------------------------------------------
    def parse_entries(self, top_level: bool) -> FoamDict:
        d = FoamDict()
        while True:
            t = self.peek()
            if t is None:
                if top_level:
                    return d
                raise ParseError("字典缺少 '}'")
            if t.text == "}":
                if top_level:
                    self.next()
                    continue  # 容忍多余的花括号
                self.next()
                return d
            if t.text == ";":
                self.next()  # 空语句
                continue
            key = self.next().text
            if key.startswith("#"):
                # 指令行: 原样保留(用 append 而不是 set, 因为同一条指令可以出现多次)
                head = key.split()[0] if key.split() else key
                d.items.append((head, Directive(key)))
                continue
            nxt = self.peek()
            if nxt is not None and nxt.text == "{":
                self.next()  # 吃掉 '{'
                value: Any = self.parse_entries(top_level=False)
            else:
                value = self.parse_value()
                if self.peek() is not None and self.peek().text == ";":
                    self.next()
            d.set(key, value)
        return d

    def parse_value(self) -> Any:
        t = self.peek()
        if t is None:
            return Compound([])
        if t.text == "{":
            self.next()  # 吃掉 '{'
            return self.parse_entries(top_level=False)
        if t.text == "[":
            dims = self.parse_dims()
            value = self.parse_value()
            return Dimensioned(dims, value)
        parts: list[Any] = []
        while True:
            t = self.peek()
            if t is None or t.text in (";", "}"):
                break
            if t.text == "(":
                parts.append(self.parse_list())
            elif t.text == "{":
                self.next()
                parts.append(self.parse_entries(top_level=False))
            elif t.text == "]":
                break
            elif t.text == "[":
                # 值在前、量纲在后: ``nu 1e-05 [m^2/s];``
                dims = self.parse_dims()
                if len(parts) == 1:
                    return Dimensioned(dims, parts[0], dims_first=False)
                parts.append(Dimensioned(dims, Compound([])))
            else:
                parts.append(self.next().text)
        if len(parts) == 1:
            return parts[0]
        return Compound(parts)

    def parse_list(self) -> FoamList:
        self.expect("(")
        out = FoamList()
        while True:
            t = self.peek()
            if t is None:
                raise ParseError("列表缺少 ')'")
            if t.text == ")":
                self.next()
                return out
            if t.text == ";":
                self.next()
                continue
            if t.text == "(":
                out.append(self.parse_list())
            elif t.text == "{":
                self.next()
                out.append(self.parse_entries(top_level=False))
            elif t.text == "[":
                dims = self.parse_dims()
                out.append(Dimensioned(dims, self.parse_value()))
            else:
                out.append(self.next().text)

    def parse_dims(self) -> list[str]:
        self.expect("[")
        dims: list[str] = []
        while True:
            t = self.next()
            if t.text == "]":
                return dims
            dims.append(t.text)


def parse_dict(text: str) -> FoamDict:
    """解析一个完整的字典文件(或片段)。"""
    return _Parser(tokenize(text)).parse_entries(top_level=True)


# 兼容别名
loads = parse_dict


# ---------------------------------------------------------------------------
# 序列化
# ---------------------------------------------------------------------------
_INLINE_LIMIT = 68


def format_value(v: Any, indent: int = 0) -> str:
    """把一个值格式化成 OpenFOAM 语法的字符串。"""
    pad = " " * indent
    if isinstance(v, str):
        return v
    if isinstance(v, Dimensioned):
        dims = "[" + " ".join(v.dims) + "]"
        val = format_value(v.value, indent).strip()
        if not val:
            return dims
        return f"{dims} {val}" if getattr(v, "dims_first", True) else f"{val} {dims}"
    if isinstance(v, FoamDict):
        return format_dict(v, indent)
    if isinstance(v, FoamList):
        if not v:
            return "()"
        if all(isinstance(x, str) for x in v) and _inline_len(v) <= _INLINE_LIMIT:
            return "(" + " ".join(v) + ")"
        inner = "\n".join(
            " " * (indent + 4) + format_value(x, indent + 4) for x in v
        )
        return "(\n" + inner + "\n" + pad + ")"
    if isinstance(v, Compound):
        if not v:
            return ""
        inline = _compound_inline(v, indent)
        if inline is not None and len(inline) <= _INLINE_LIMIT:
            return inline
        # 多行: 原子留在第一行, 列表/字典换行
        head: list[str] = []
        tail: list[str] = []
        for x in v:
            if isinstance(x, str):
                head.append(x)
            else:
                tail.append(format_value(x, indent + 4))
        lines = []
        if head:
            lines.append(" ".join(head))
        lines.extend(tail)
        return "\n".join(lines)
    return str(v)


def _inline_len(items: Iterable[Any]) -> int:
    total = 0
    for x in items:
        total += len(x) if isinstance(x, str) else _INLINE_LIMIT + 1
    return total


def _compound_inline(v: Compound, indent: int) -> str | None:
    """能一行写完就返回字符串, 否则返回 None。"""
    out: list[str] = []
    for x in v:
        if isinstance(x, str):
            out.append(x)
        elif isinstance(x, FoamList):
            if not all(isinstance(y, str) for y in x):
                return None
            if _inline_len(x) > _INLINE_LIMIT:
                return None
            out.append("(" + " ".join(x) + ")")
        else:
            return None
    # ``nonuniform List<scalar> 100 (...)`` 这种要贴在一起写
    text = ""
    for i, part in enumerate(out):
        if i == 0:
            text = part
        elif part.startswith("(") and out[i - 1].lstrip("-").isdigit():
            text += part
        else:
            text += " " + part
    return text


def format_body(d: FoamDict, indent: int = 0) -> str:
    """把字典渲染成文件正文(不加大括号, 顶层条目不缩进)。"""
    lines = [format_entry(k, v, indent) for k, v in d.items]
    return "\n".join(lines)


def format_dict(d: FoamDict, indent: int = 0) -> str:
    """把字典格式化成 ``{ ... }`` 形式(不含文件头)。"""
    pad = " " * indent
    lines: list[str] = [pad + "{"]
    for key, value in d.items:
        lines.append(" " * (indent + 4) + format_entry(key, value, indent + 4))
    lines.append(pad + "}")
    return "\n".join(lines)


def format_entry(key: str, value: Any, indent: int = 0) -> str:
    pad = " " * indent
    if isinstance(value, Directive):
        return pad + value.text
    if isinstance(value, FoamDict):
        return f"{key}\n" + format_dict(value, indent)
    body = format_value(value, indent)
    if "\n" in body:
        return f"{key} " + body + ";"
    if key.startswith("#"):
        return f"{key} {body}".rstrip()
    return f"{key} {body};"


def dump(d: FoamDict) -> str:
    """序列化一个字典(不含 FoamFile 头)。"""
    return format_dict(d, 0)


# ---------------------------------------------------------------------------
# 取值辅助函数
# ---------------------------------------------------------------------------
def atom(v: Any) -> str | None:
    """如果值是单个原子则返回它的文本, 否则返回 None。"""
    if isinstance(v, str):
        return v
    if isinstance(v, Compound) and len(v) == 1 and isinstance(v[0], str):
        return v[0]
    return None


def atoms(v: Any) -> list[str]:
    """把值摊平成一个原子文本列表(忽略嵌套结构)。"""
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    if isinstance(v, Dimensioned):
        return atoms(v.value)
    if isinstance(v, (FoamList, Compound)):
        out: list[str] = []
        for x in v:
            out.extend(atoms(x))
        return out
    return [str(v)]


def get_atom(d: FoamDict | None, key: str, default: str | None = None) -> str | None:
    if d is None:
        return default
    v = d.get(key, _MISSING)
    if v is _MISSING:
        return default
    a = atom(v)
    if a is not None:
        return a
    return " ".join(atoms(v))


def get_dict(d: FoamDict | None, key: str) -> FoamDict | None:
    if d is None:
        return None
    v = d.get(key)
    return v if isinstance(v, FoamDict) else None


def get_list(d: FoamDict | None, key: str) -> FoamList | None:
    if d is None:
        return None
    v = d.get(key)
    return v if isinstance(v, FoamList) else None


def as_float(text: Any, default: float | None = None) -> float | None:
    if text is None:
        return default
    if isinstance(text, (int, float)):
        return float(text)
    s = str(text).strip().rstrip(";")
    if s.endswith(")"):
        s = s[:-1]
    try:
        return float(s)
    except ValueError:
        return default


def as_int(text: Any, default: int | None = None) -> int | None:
    f = as_float(text)
    if f is None:
        return default
    return int(round(f))


def make_uniform(tokens: Iterable[str]) -> Compound:
    """构造 ``uniform <value>`` 形式的值。"""
    toks = [str(t) for t in tokens]
    if len(toks) == 1:
        return Compound(["uniform", toks[0]])
    return Compound(["uniform", FoamList(toks)])


def uniform_parts(v: Any) -> tuple[bool, list[str]]:
    """解析 ``uniform ...`` 值。

    返回 ``(是否 uniform, token 列表)``。若值不是 uniform 形式(例如
    ``$internalField`` 或 nonuniform 列表), 第一个元素为 False。
    """
    if isinstance(v, Compound) and v and isinstance(v[0], str) and v[0] == "uniform":
        return True, atoms(Compound(list(v[1:])))
    if isinstance(v, str) and v.startswith("$"):
        return False, [v]
    return False, atoms(v)


# ---------------------------------------------------------------------------
# 文件读写
# ---------------------------------------------------------------------------
def load(path: str) -> tuple[FoamDict, FoamDict]:
    """读取字典文件。

    返回 ``(FoamFile 头字典, 正文字典)``; 若文件没有 FoamFile 头,
    头字典为空。
    """
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    return parse_file(text)


def parse_file(text: str) -> tuple[FoamDict, FoamDict]:
    root = parse_dict(text)
    header = get_dict(root, "FoamFile")
    if header is not None:
        del root["FoamFile"]
    return (header if header is not None else FoamDict()), root
