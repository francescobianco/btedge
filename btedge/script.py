"""The snippet language: a very small JavaScript look-alike, interpreted here.

Snippets are written in the panel and run on every node (see automation.py), so the
language can do nothing by itself: no loops, no imports, no access to Python. A snippet
can only call what its host hands it as globals, and every run has a step budget.

    let near = -70                              // let / const / var, numbers, strings,
    host.onDevice(device => {                   // true / false / null, [lists]
      if (device.name == "MSF0" && device.rssi > near) {
        device.action("vibrate")                // if / else, return
      } else {
        log("seen", device.address)             // + - * / %  == != < <= > >=  && || !
      }
    })
    host.onReady = function () { log("ready on " + host.name) }

Functions are arrow functions or `function (a, b) { ... }`; `x.onEvent = fn` is the same
as `x.onEvent(fn)`. Strings have length, startsWith, endsWith, includes, toLowerCase,
toUpperCase and trim; lists have length and includes.
"""
import re

MAX_STEPS = 20000     # evaluations per run: there are no loops, this bounds recursion
MAX_DEPTH = 40        # nested calls
MAX_STRING = 10000

KEYWORDS = {"let", "const", "var", "if", "else", "return", "function", "true", "false", "null"}
TOKEN = re.compile(r"""
    (?P<space>\s+|//[^\n]*|/\*.*?\*/)
  | (?P<number>\d+(?:\.\d+)?)
  | (?P<string>"(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*')
  | (?P<name>[A-Za-z_$][\w$]*)
  | (?P<op>=>|===|!==|==|!=|<=|>=|&&|\|\||[-+*/%!<>=(){}\[\],;.])
""", re.S | re.X)
ESCAPES = {"n": "\n", "t": "\t"}


class ScriptError(Exception):
    def __init__(self, message, line=None):
        super().__init__("riga %d: %s" % (line, message) if line else message)


class _Return(Exception):
    def __init__(self, value):
        self.value = value


def tokenize(source):
    tokens, pos, line = [], 0, 1
    while pos < len(source):
        match = TOKEN.match(source, pos)
        if match is None:
            raise ScriptError("carattere inatteso %r" % source[pos], line)
        kind, text = match.lastgroup, match.group()
        if kind == "number":
            tokens.append(("number", float(text) if "." in text else int(text), line))
        elif kind == "string":
            tokens.append(("string", re.sub(r"\\(.)", lambda m: ESCAPES.get(m.group(1), m.group(1)), text[1:-1]), line))
        elif kind == "name":
            tokens.append(("keyword" if text in KEYWORDS else "name", text, line))
        elif kind == "op":
            tokens.append(("op", text, line))
        line += text.count("\n")
        pos = match.end()
    tokens.append(("end", None, line))
    return tokens


class Parser:
    """Recursive descent; nodes are tuples (kind, line, ...)."""

    BINARY = [("||",), ("&&",), ("==", "!=", "===", "!=="), ("<", "<=", ">", ">="), ("+", "-"), ("*", "/", "%")]

    def __init__(self, source):
        self.tokens = tokenize(source)
        self.pos = 0

    def peek(self, offset=0):
        return self.tokens[min(self.pos + offset, len(self.tokens) - 1)]

    def at(self, value, offset=0):
        kind, text, _ = self.peek(offset)
        return kind in ("op", "keyword") and text == value

    def take(self, value=None):
        token = self.peek()
        if value is not None and not self.at(value):
            found = "fine dello script" if token[0] == "end" else repr(str(token[1]))
            raise ScriptError("atteso %r, trovato %s" % (value, found), token[2])
        self.pos += 1
        return token

    def name(self, anything=False):
        kind, text, line = self.peek()
        if kind != "name" and not (anything and kind == "keyword"):
            raise ScriptError("atteso un nome", line)
        self.pos += 1
        return text

    def program(self):
        body = []
        while self.peek()[0] != "end":
            body.append(self.statement())
        return body

    def block(self):
        self.take("{")
        body = []
        while not self.at("}"):
            if self.peek()[0] == "end":
                raise ScriptError("manca '}'", self.peek()[2])
            body.append(self.statement())
        self.take("}")
        return body

    def statement(self):
        line = self.peek()[2]
        if self.at("{"):
            return ("block", line, self.block())
        if self.at("function") and self.peek(1)[0] == "name":  # function name(...) { }
            name = self.peek(1)[1]
            return ("let", line, name, self.primary())
        if self.at("let") or self.at("const") or self.at("var"):
            self.take()
            name = self.name()
            value = None
            if self.at("="):
                self.take()
                value = self.expression()
            node = ("let", line, name, value)
        elif self.at("if"):
            self.take()
            self.take("(")
            test = self.expression()
            self.take(")")
            then = self.statement()
            other = None
            if self.at("else"):
                self.take()
                other = self.statement()
            return ("if", line, test, then, other)
        elif self.at("return"):
            self.take()
            value = None if self.at(";") or self.at("}") or self.peek()[0] == "end" else self.expression()
            node = ("return", line, value)
        else:
            node = self.expression()
            if self.at("="):
                if node[0] not in ("name", "member"):
                    raise ScriptError("non si può assegnare qui", line)
                self.take()
                node = ("assign", line, node, self.expression())
        if self.at(";"):
            self.take()
        return node

    def expression(self, level=0):
        if level == len(self.BINARY):
            return self.unary()
        left = self.expression(level + 1)
        while any(self.at(op) for op in self.BINARY[level]):
            _, op, line = self.take()
            left = ("binary", line, op, left, self.expression(level + 1))
        return left

    def unary(self):
        if self.at("!") or self.at("-"):
            _, op, line = self.take()
            return ("unary", line, op, self.unary())
        node = self.primary()
        while True:
            line = self.peek()[2]
            if self.at("."):
                self.take()
                node = ("member", line, node, self.name(anything=True))
            elif self.at("("):
                node = ("call", line, node, self.arguments("(", ")"))
            elif self.at("["):
                self.take()
                node = ("index", line, node, self.expression())
                self.take("]")
            else:
                return node

    def arguments(self, opening, closing):
        self.take(opening)
        items = []
        while not self.at(closing):
            items.append(self.expression())
            if not self.at(closing) and not self.at(","):
                self.take(closing)  # fails saying which bracket is missing
            if self.at(","):
                self.take()
        self.take(closing)
        return items

    def arrow_ahead(self):
        """Is the "(" here the parameter list of an arrow function?"""
        depth, offset = 0, 0
        while True:
            kind, text, _ = self.peek(offset)
            if kind == "end":
                return False
            if kind == "op" and text == "(":
                depth += 1
            elif kind == "op" and text == ")":
                depth -= 1
                if depth == 0:
                    return self.at("=>", offset + 1)
            offset += 1

    def parameters(self):
        self.take("(")
        names = []
        while not self.at(")"):
            names.append(self.name())
            if not self.at(")"):
                self.take(",")
        self.take(")")
        return names

    def primary(self):
        kind, text, line = self.peek()
        if kind in ("number", "string"):
            self.take()
            return ("value", line, text)
        if kind == "keyword" and text in ("true", "false", "null"):
            self.take()
            return ("value", line, {"true": True, "false": False, "null": None}[text])
        if kind == "keyword" and text == "function":
            self.take()
            if self.peek()[0] == "name":
                self.take()
            params = self.parameters()
            return ("function", line, params, self.block())
        if kind == "name":
            if self.at("=>", 1):
                self.take()
                return self.arrow(line, [text])
            self.take()
            return ("name", line, text)
        if self.at("(") and self.arrow_ahead():
            return self.arrow(line, self.parameters())
        if self.at("("):
            self.take()
            node = self.expression()
            self.take(")")
            return node
        if self.at("["):
            return ("list", line, self.arguments("[", "]"))
        raise ScriptError("fine dello script inattesa" if kind == "end" else "inatteso %r" % str(text), line)

    def arrow(self, line, params):
        self.take("=>")
        if self.at("{"):
            return ("function", line, params, self.block())
        return ("function", line, params, [("return", line, self.expression())])


def parse(source):
    """Syntax tree of a snippet; raises ScriptError."""
    try:
        return Parser(source).program()
    except RecursionError:
        raise ScriptError("script troppo annidato") from None


class Function:
    def __init__(self, params, body, env):
        self.params, self.body, self.env = params, body, env


class Native:
    """A function offered by the host: called with the list of arguments."""

    def __init__(self, call):
        self.call = call


class Object:
    """A host object: fixed properties; `obj.x = value` goes to assign(name, value) if given."""

    def __init__(self, kind, props, assign=None):
        self.kind, self.props, self.assign = kind, props, assign


def truthy(value):
    return value is not None and value is not False and value != 0 and value != ""


def text(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value == int(value):
        return str(int(value))
    if isinstance(value, list):
        return ",".join(text(v) for v in value)
    if isinstance(value, (Function, Native)):
        return "[function]"
    if isinstance(value, Object):
        return "[%s]" % value.kind
    return str(value)


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def equal(a, b):
    if _number(a) and _number(b):
        return a == b
    return type(a) is type(b) and a == b


STRING_METHODS = {
    "startsWith": lambda s, a: s.startswith(text(a[0])) if a else False,
    "endsWith": lambda s, a: s.endswith(text(a[0])) if a else False,
    "includes": lambda s, a: text(a[0]) in s if a else False,
    "toLowerCase": lambda s, a: s.lower(),
    "toUpperCase": lambda s, a: s.upper(),
    "trim": lambda s, a: s.strip(),
}


class Interpreter:
    def __init__(self, globals_=None):
        self.globals = dict(globals_ or {})
        self.steps = 0
        self.depth = 0

    def run(self, tree):
        """Run a program (the top level of a snippet)."""
        return self._guarded(lambda: self.execute(tree, [self.globals]))

    def call(self, function, args=()):
        """Call a snippet function from outside, with a fresh step budget."""
        return self._guarded(lambda: self.invoke(function, list(args), None))

    def _guarded(self, work):
        self.steps, self.depth = MAX_STEPS, 0
        try:
            return work()
        except _Return as stop:
            return stop.value
        except RecursionError:
            raise ScriptError("troppe chiamate annidate") from None

    # -- statements ---------------------------------------------------------

    def execute(self, body, env):
        for node in body:
            self.statement(node, env)

    def statement(self, node, env):
        kind, line = node[0], node[1]
        if kind == "let":
            env[-1][node[2]] = None if node[3] is None else self.eval(node[3], env)
        elif kind == "if":
            if truthy(self.eval(node[2], env)):
                self.statement(node[3], env)
            elif node[4] is not None:
                self.statement(node[4], env)
        elif kind == "block":
            self.execute(node[2], env)
        elif kind == "return":
            raise _Return(None if node[2] is None else self.eval(node[2], env))
        elif kind == "assign":
            target, value = node[2], self.eval(node[3], env)
            if target[0] == "name":
                scope = next((s for s in reversed(env) if target[2] in s), None)
                if scope is None or scope is self.globals and isinstance(scope[target[2]], (Object, Native)):
                    raise ScriptError("variabile %r non dichiarata (usa let)" % target[2], line)
                scope[target[2]] = value
            else:
                obj = self.eval(target[2], env)
                if not isinstance(obj, Object) or obj.assign is None:
                    raise ScriptError("non si può assegnare %r" % target[3], line)
                obj.assign(target[3], value, line)
        else:
            self.eval(node, env)

    # -- expressions --------------------------------------------------------

    def eval(self, node, env):
        self.steps -= 1
        if self.steps < 0:
            raise ScriptError("troppi passi: lo script è stato fermato", node[1])
        kind, line = node[0], node[1]
        if kind == "value":
            return node[2]
        if kind == "name":
            for scope in reversed(env):
                if node[2] in scope:
                    return scope[node[2]]
            raise ScriptError("%r non è definito" % node[2], line)
        if kind == "function":
            return Function(node[2], node[3], env)
        if kind == "list":
            return [self.eval(item, env) for item in node[2]]
        if kind == "unary":
            value = self.eval(node[3], env)
            if node[2] == "!":
                return not truthy(value)
            if not _number(value):
                raise ScriptError("'-' vuole un numero", line)
            return -value
        if kind == "binary":
            return self.binary(node, env)
        if kind == "member":
            return self.member(self.eval(node[2], env), node[3], line)
        if kind == "index":
            target, index = self.eval(node[2], env), self.eval(node[3], env)
            if isinstance(target, (list, str)) and isinstance(index, int) and not isinstance(index, bool):
                return target[index] if 0 <= index < len(target) else None
            raise ScriptError("indice non valido", line)
        if kind == "call":
            function = self.eval(node[2], env)
            return self.invoke(function, [self.eval(arg, env) for arg in node[3]], line)
        raise ScriptError("istruzione non valida qui", line)

    def binary(self, node, env):
        op, line = node[2], node[1]
        left = self.eval(node[3], env)
        if op == "&&":
            return self.eval(node[4], env) if truthy(left) else left
        if op == "||":
            return left if truthy(left) else self.eval(node[4], env)
        right = self.eval(node[4], env)
        if op in ("==", "==="):
            return equal(left, right)
        if op in ("!=", "!=="):
            return not equal(left, right)
        if op == "+" and (isinstance(left, str) or isinstance(right, str)):
            result = text(left) + text(right)
            if len(result) > MAX_STRING:
                raise ScriptError("stringa troppo lunga", line)
            return result
        if _number(left) and _number(right):
            if op in ("/", "%") and right == 0:
                raise ScriptError("divisione per zero", line)
            return {"+": lambda: left + right, "-": lambda: left - right, "*": lambda: left * right,
                    "/": lambda: left / right, "%": lambda: left % right, "<": lambda: left < right,
                    "<=": lambda: left <= right, ">": lambda: left > right, ">=": lambda: left >= right}[op]()
        if op in ("<", "<=", ">", ">="):
            if isinstance(left, str) and isinstance(right, str):
                return {"<": left < right, "<=": left <= right, ">": left > right, ">=": left >= right}[op]
            return False  # e.g. a missing rssi (null) is never "> -70"
        raise ScriptError("%r vuole due numeri" % op, line)

    def member(self, target, name, line):
        if isinstance(target, Object):
            if name in target.props:
                return target.props[name]
            raise ScriptError("%s non ha %r" % (target.kind, name), line)
        if isinstance(target, (str, list)) and name == "length":
            return len(target)
        if isinstance(target, str) and name in STRING_METHODS:
            return Native(lambda args: STRING_METHODS[name](target, args))
        if isinstance(target, list) and name == "includes":
            return Native(lambda args: any(equal(item, args[0]) for item in target) if args else False)
        raise ScriptError("impossibile leggere %r di %s" % (name, text(target)), line)

    def invoke(self, function, args, line):
        if isinstance(function, Native):
            try:
                return function.call(args)
            except ScriptError:
                raise
            except (TypeError, ValueError, IndexError) as exc:
                raise ScriptError("argomenti non validi: %s" % exc, line) from None
        if not isinstance(function, Function):
            raise ScriptError("%s non è una funzione" % text(function), line)
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise ScriptError("troppe chiamate annidate", line)
        scope = {name: args[i] if i < len(args) else None for i, name in enumerate(function.params)}
        try:
            self.execute(function.body, function.env + [scope])
        except _Return as stop:
            return stop.value
        finally:
            self.depth -= 1
        return None
