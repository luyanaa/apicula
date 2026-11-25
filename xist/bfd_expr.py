"""Parser and evaluator for the vendor BFD Boolean expressions.

The BFD equations combine routing arcs with configuration predicates::

    fuse = (!arcval(JPADDIA_PIO,JDIA) && wire(JDIA));
    fuse = (property(PIO_A,IOBUF.IO_TYPE,"LVCMOS25") && config(PIO_A,PADDI,"PADDI"));

The grammar is tiny: ``&&``, ``||``, ``!``, parentheses, ``TRUE``/``FALSE`` and
the predicates ``property``, ``config``, ``comp``, ``wire`` and ``arcval``.
"""

from __future__ import annotations

import re

_TOKEN_RE = re.compile(
    r'\s*(&&|\|\||!|\(|\)|,|"([^"]*)"|([A-Za-z_][A-Za-z0-9_.]*)|(-?\d+(?:\.\d+)?))')

_TRUE_WORDS = ("TRUE", "FALSE")


def tokenize(text: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    i = 0
    while i < len(text):
        m = _TOKEN_RE.match(text, i)
        if not m:
            raise ValueError(f"bad token at {i}: {text[i:i + 30]!r}")
        i = m.end()
        if m.group(2) is not None:
            out.append(("str", m.group(2)))
        elif m.group(3) is not None:
            out.append(("id", m.group(3)))
        elif m.group(4) is not None:
            out.append(("num", m.group(4)))
        else:
            out.append(("op", m.group(1)))
    return out


class _Parser:
    def __init__(self, toks):
        self.toks = toks
        self.i = 0

    def peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else (None, None)

    def eat(self, value=None):
        kind, tok = self.peek()
        if value is not None and tok != value:
            raise ValueError(f"expected {value!r}, got {tok!r}")
        self.i += 1
        return kind, tok

    def expr(self):
        node = self.and_()
        while self.peek() == ("op", "||"):
            self.eat()
            node = ("or", node, self.and_())
        return node

    def and_(self):
        node = self.unary()
        while self.peek() == ("op", "&&"):
            self.eat()
            node = ("and", node, self.unary())
        return node

    def unary(self):
        if self.peek() == ("op", "!"):
            self.eat()
            return ("not", self.unary())
        if self.peek() == ("op", "("):
            self.eat()
            node = self.expr()
            self.eat(")")
            return node
        kind, tok = self.peek()
        if kind == "id" and tok in _TRUE_WORDS:
            self.eat()
            return ("const", tok == "TRUE")
        if kind == "id":
            self.eat()
            args = []
            if self.peek() == ("op", "("):
                self.eat("(")
                if self.peek() != ("op", ")"):
                    while True:
                        ak, av = self.peek()
                        if ak in ("str", "num", "id"):
                            self.eat()
                            args.append(av)
                        else:
                            raise ValueError(f"bad argument {av!r}")
                        if self.peek() == ("op", ","):
                            self.eat(",")
                        else:
                            break
                self.eat(")")
            return ("pred", tok, args)
        raise ValueError(f"bad unary {self.peek()!r}")


def parse(text: str):
    p = _Parser(tokenize(text))
    node = p.expr()
    if p.i != len(p.toks):
        raise ValueError(f"trailing tokens: {p.toks[p.i:]}")
    return node


def evaluate(node, env: dict) -> bool:
    """Evaluate a parsed expression with an environment.

    env keys: ``props`` (site, field) -> value; ``configs`` site -> {(port,
    value)}; ``sites`` set; ``wires`` set; ``arcs`` set of (src, dst).
    """
    op = node[0]
    if op == "const":
        return node[1]
    if op == "not":
        return not evaluate(node[1], env)
    if op == "and":
        return evaluate(node[1], env) and evaluate(node[2], env)
    if op == "or":
        return evaluate(node[1], env) or evaluate(node[2], env)
    name, args = node[1], node[2]
    if name == "property":
        return env.get("props", {}).get((args[0], args[1])) == args[2]
    if name == "config":
        return (args[1], args[2]) in env.get("configs", {}).get(args[0], set())
    if name == "comp":
        return args[0] in env.get("sites", set())
    if name == "wire":
        return args[0] in env.get("wires", set())
    if name == "arcval":
        return (args[0], args[1]) in env.get("arcs", set())
    return False


_CACHE: dict[str, object] = {}


def parse_cached(text: str):
    node = _CACHE.get(text)
    if node is None:
        node = parse(text)
        _CACHE[text] = node
    return node
