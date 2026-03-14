"""
HIDScript parser and executor for Loki.

Supports P4wnP1-compatible HIDScript syntax:
  - Function calls: type("hello"); press("GUI r"); delay(500);
  - var declarations: var x = 1;
  - for / while loops
  - if / else conditionals
  - // and /* */ comments
  - String concatenation with +
  - Basic arithmetic (+, -, *, /)
  - console.log() for job output

Zero external dependencies — pure Python DSL parser.
"""
import re
import time
import logging
from threading import Event

from logger import Logger

logger = Logger(name="loki.hidscript", level=logging.DEBUG)

# ── LED constants (available in scripts) ──────────────────────
NUM = 0x01
CAPS = 0x02
SCROLL = 0x04
ANY = 0xFF

# ── Mouse button constants ────────────────────────────────────
BT1 = 1      # Left
BT2 = 2      # Right
BT3 = 4      # Middle
BTNONE = 0


class HIDScriptError(Exception):
    """Error during HIDScript execution."""
    def __init__(self, message, line=None):
        self.line = line
        super().__init__(f"Line {line}: {message}" if line else message)


class HIDScriptParser:
    """Parse and execute P4wnP1-compatible HIDScript."""

    def __init__(self, hid_controller, layout="us"):
        self.hid = hid_controller
        self._default_layout = layout
        self._output = []  # console.log output

    def execute(self, source: str, stop_event: Event = None, job_id: str = ""):
        """Parse and execute a HIDScript source string.

        Returns list of console.log output lines.
        """
        self._output = []
        self._stop = stop_event or Event()
        self._vars = {
            # Built-in constants
            "NUM": NUM, "CAPS": CAPS, "SCROLL": SCROLL, "ANY": ANY,
            "BT1": BT1, "BT2": BT2, "BT3": BT3, "BTNONE": BTNONE,
            "true": True, "false": False, "null": None,
        }

        # Strip comments
        source = self._strip_comments(source)
        # Tokenize into statements
        stmts = self._parse_block(source)
        # Execute
        self._exec_stmts(stmts)

        return self._output

    # ── Comment stripping ──────────────────────────────────────

    def _strip_comments(self, source: str) -> str:
        """Remove // and /* */ comments."""
        # Block comments first
        source = re.sub(r'/\*.*?\*/', '', source, flags=re.DOTALL)
        # Line comments
        source = re.sub(r'//[^\n]*', '', source)
        return source

    # ── Parser ─────────────────────────────────────────────────

    def _parse_block(self, source: str) -> list:
        """Parse source into a list of statement dicts."""
        stmts = []
        pos = 0
        source = source.strip()

        while pos < len(source):
            if self._stop.is_set():
                break
            pos = self._skip_ws(source, pos)
            if pos >= len(source):
                break

            # var declaration
            if source[pos:pos+4] == 'var ' or source[pos:pos+4] == 'let ':
                end = source.find(';', pos)
                if end == -1:
                    end = len(source)
                decl = source[pos+4:end].strip()
                eq = decl.find('=')
                if eq >= 0:
                    name = decl[:eq].strip()
                    value_expr = decl[eq+1:].strip()
                    stmts.append({"type": "assign", "name": name, "expr": value_expr})
                else:
                    stmts.append({"type": "assign", "name": decl.strip(), "expr": "null"})
                pos = end + 1

            # for loop
            elif source[pos:pos+4] == 'for ' or source[pos:pos+4] == 'for(':
                stmt, pos = self._parse_for(source, pos)
                stmts.append(stmt)

            # while loop
            elif source[pos:pos+6] == 'while ' or source[pos:pos+6] == 'while(':
                stmt, pos = self._parse_while(source, pos)
                stmts.append(stmt)

            # if statement
            elif source[pos:pos+3] == 'if ' or source[pos:pos+3] == 'if(':
                stmt, pos = self._parse_if(source, pos)
                stmts.append(stmt)

            # Block: { ... }
            elif source[pos] == '{':
                end = self._find_matching_brace(source, pos)
                inner = source[pos+1:end]
                stmts.extend(self._parse_block(inner))
                pos = end + 1

            # Expression statement (function call or assignment)
            else:
                end = source.find(';', pos)
                if end == -1:
                    end = len(source)
                expr = source[pos:end].strip()
                if expr:
                    # Check for assignment: name = expr
                    m = re.match(r'^([a-zA-Z_]\w*)\s*=\s*(.+)$', expr)
                    if m and not expr.startswith('=='):
                        stmts.append({"type": "assign", "name": m.group(1), "expr": m.group(2)})
                    else:
                        stmts.append({"type": "expr", "expr": expr})
                pos = end + 1

        return stmts

    def _parse_for(self, source, pos):
        """Parse: for (init; cond; incr) { body }"""
        # Find parenthesized header
        p_start = source.index('(', pos)
        p_end = self._find_matching_paren(source, p_start)
        header = source[p_start+1:p_end]

        parts = header.split(';')
        if len(parts) != 3:
            raise HIDScriptError("Invalid for loop header")
        init_expr = parts[0].strip()
        cond_expr = parts[1].strip()
        incr_expr = parts[2].strip()

        # Remove var/let prefix from init
        for prefix in ('var ', 'let '):
            if init_expr.startswith(prefix):
                init_expr = init_expr[len(prefix):]

        # Find body
        body_start = self._skip_ws(source, p_end + 1)
        if body_start < len(source) and source[body_start] == '{':
            body_end = self._find_matching_brace(source, body_start)
            body = source[body_start+1:body_end]
            next_pos = body_end + 1
        else:
            semi = source.find(';', body_start)
            if semi == -1:
                semi = len(source)
            body = source[body_start:semi]
            next_pos = semi + 1

        return {
            "type": "for",
            "init": init_expr,
            "cond": cond_expr,
            "incr": incr_expr,
            "body": body,
        }, next_pos

    def _parse_while(self, source, pos):
        """Parse: while (cond) { body }"""
        p_start = source.index('(', pos)
        p_end = self._find_matching_paren(source, p_start)
        cond = source[p_start+1:p_end].strip()

        body_start = self._skip_ws(source, p_end + 1)
        if body_start < len(source) and source[body_start] == '{':
            body_end = self._find_matching_brace(source, body_start)
            body = source[body_start+1:body_end]
            next_pos = body_end + 1
        else:
            semi = source.find(';', body_start)
            if semi == -1:
                semi = len(source)
            body = source[body_start:semi]
            next_pos = semi + 1

        return {"type": "while", "cond": cond, "body": body}, next_pos

    def _parse_if(self, source, pos):
        """Parse: if (cond) { body } [else { body }]"""
        p_start = source.index('(', pos)
        p_end = self._find_matching_paren(source, p_start)
        cond = source[p_start+1:p_end].strip()

        body_start = self._skip_ws(source, p_end + 1)
        if body_start < len(source) and source[body_start] == '{':
            body_end = self._find_matching_brace(source, body_start)
            body = source[body_start+1:body_end]
            next_pos = body_end + 1
        else:
            semi = source.find(';', body_start)
            if semi == -1:
                semi = len(source)
            body = source[body_start:semi]
            next_pos = semi + 1

        # Check for else
        else_body = None
        check = self._skip_ws(source, next_pos)
        if source[check:check+4] == 'else':
            after_else = self._skip_ws(source, check + 4)
            if after_else < len(source) and source[after_else] == '{':
                eb_end = self._find_matching_brace(source, after_else)
                else_body = source[after_else+1:eb_end]
                next_pos = eb_end + 1
            elif source[after_else:after_else+2] == 'if':
                # else if — parse recursively
                inner_if, next_pos = self._parse_if(source, after_else)
                else_body = inner_if  # will be a dict, handle in exec
            else:
                semi = source.find(';', after_else)
                if semi == -1:
                    semi = len(source)
                else_body = source[after_else:semi]
                next_pos = semi + 1

        return {"type": "if", "cond": cond, "body": body, "else": else_body}, next_pos

    # ── Executor ───────────────────────────────────────────────

    def _exec_stmts(self, stmts: list):
        """Execute a list of parsed statements."""
        for stmt in stmts:
            if self._stop.is_set():
                return
            stype = stmt["type"]

            if stype == "assign":
                self._vars[stmt["name"]] = self._eval_expr(stmt["expr"])

            elif stype == "expr":
                self._eval_expr(stmt["expr"])

            elif stype == "for":
                self._exec_for(stmt)

            elif stype == "while":
                self._exec_while(stmt)

            elif stype == "if":
                self._exec_if(stmt)

    def _exec_for(self, stmt):
        """Execute a for loop."""
        # Parse init as assignment
        init = stmt["init"]
        eq = init.find('=')
        if eq >= 0:
            name = init[:eq].strip()
            self._vars[name] = self._eval_expr(init[eq+1:].strip())

        max_iterations = 100000
        i = 0
        while i < max_iterations:
            if self._stop.is_set():
                return
            if not self._eval_expr(stmt["cond"]):
                break
            self._exec_stmts(self._parse_block(stmt["body"]))
            # Execute increment
            incr = stmt["incr"]
            if "++" in incr:
                var_name = incr.replace("++", "").strip()
                self._vars[var_name] = self._vars.get(var_name, 0) + 1
            elif "--" in incr:
                var_name = incr.replace("--", "").strip()
                self._vars[var_name] = self._vars.get(var_name, 0) - 1
            else:
                eq = incr.find('=')
                if eq >= 0:
                    name = incr[:eq].strip()
                    self._vars[name] = self._eval_expr(incr[eq+1:].strip())
            i += 1

    def _exec_while(self, stmt):
        """Execute a while loop."""
        max_iterations = 1000000
        i = 0
        while i < max_iterations:
            if self._stop.is_set():
                return
            if not self._eval_expr(stmt["cond"]):
                break
            self._exec_stmts(self._parse_block(stmt["body"]))
            i += 1

    def _exec_if(self, stmt):
        """Execute an if/else statement."""
        if self._eval_expr(stmt["cond"]):
            self._exec_stmts(self._parse_block(stmt["body"]))
        elif stmt.get("else"):
            else_part = stmt["else"]
            if isinstance(else_part, dict):
                # else if
                self._exec_if(else_part)
            else:
                self._exec_stmts(self._parse_block(else_part))

    # ── Expression Evaluator ───────────────────────────────────

    def _eval_expr(self, expr):
        """Evaluate an expression string and return its value."""
        if isinstance(expr, (int, float, bool)):
            return expr
        if not isinstance(expr, str):
            return expr

        expr = expr.strip()
        if not expr:
            return None

        # String literal
        if (expr.startswith('"') and expr.endswith('"')) or \
           (expr.startswith("'") and expr.endswith("'")):
            return self._unescape(expr[1:-1])

        # Numeric literal
        try:
            if '.' in expr:
                return float(expr)
            return int(expr)
        except ValueError:
            pass

        # Boolean / null
        if expr == 'true':
            return True
        if expr == 'false':
            return False
        if expr == 'null':
            return None

        # String concatenation with +
        if self._has_top_level_op(expr, '+') and self._contains_string(expr):
            parts = self._split_top_level(expr, '+')
            result = ""
            for p in parts:
                val = self._eval_expr(p.strip())
                result += str(val) if val is not None else ""
            return result

        # Comparison operators
        for op in ['===', '!==', '==', '!=', '>=', '<=', '>', '<']:
            if self._has_top_level_op(expr, op):
                parts = self._split_top_level(expr, op, max_splits=1)
                if len(parts) == 2:
                    left = self._eval_expr(parts[0].strip())
                    right = self._eval_expr(parts[1].strip())
                    if op in ('==', '==='):
                        return left == right
                    elif op in ('!=', '!=='):
                        return left != right
                    elif op == '>':
                        return left > right
                    elif op == '<':
                        return left < right
                    elif op == '>=':
                        return left >= right
                    elif op == '<=':
                        return left <= right

        # Logical operators
        if self._has_top_level_op(expr, '&&'):
            parts = self._split_top_level(expr, '&&', max_splits=1)
            return self._eval_expr(parts[0]) and self._eval_expr(parts[1])
        if self._has_top_level_op(expr, '||'):
            parts = self._split_top_level(expr, '||', max_splits=1)
            return self._eval_expr(parts[0]) or self._eval_expr(parts[1])

        # Arithmetic
        for op in ['+', '-']:
            if self._has_top_level_op(expr, op) and not self._contains_string(expr):
                parts = self._split_top_level(expr, op)
                result = self._eval_expr(parts[0].strip())
                for p in parts[1:]:
                    val = self._eval_expr(p.strip())
                    if op == '+':
                        result = (result or 0) + (val or 0)
                    else:
                        result = (result or 0) - (val or 0)
                return result

        for op in ['*', '/']:
            if self._has_top_level_op(expr, op):
                parts = self._split_top_level(expr, op)
                result = self._eval_expr(parts[0].strip())
                for p in parts[1:]:
                    val = self._eval_expr(p.strip())
                    if op == '*':
                        result = (result or 0) * (val or 0)
                    else:
                        result = (result or 0) / (val or 1)
                return result

        # Modulo
        if self._has_top_level_op(expr, '%'):
            parts = self._split_top_level(expr, '%')
            result = self._eval_expr(parts[0].strip())
            for p in parts[1:]:
                val = self._eval_expr(p.strip())
                result = (result or 0) % (val or 1)
            return result

        # Negation
        if expr.startswith('!'):
            return not self._eval_expr(expr[1:])

        # Parenthesized expression
        if expr.startswith('(') and self._find_matching_paren(expr, 0) == len(expr) - 1:
            return self._eval_expr(expr[1:-1])

        # Function call
        m = re.match(r'^([a-zA-Z_][\w.]*)\s*\(', expr)
        if m:
            func_name = m.group(1)
            p_start = expr.index('(')
            p_end = self._find_matching_paren(expr, p_start)
            args_str = expr[p_start+1:p_end]
            args = self._parse_args(args_str)
            return self._call_func(func_name, args)

        # Variable reference
        if re.match(r'^[a-zA-Z_]\w*$', expr):
            return self._vars.get(expr, 0)

        # Increment/decrement as expression
        if expr.endswith('++'):
            name = expr[:-2].strip()
            val = self._vars.get(name, 0)
            self._vars[name] = val + 1
            return val
        if expr.endswith('--'):
            name = expr[:-2].strip()
            val = self._vars.get(name, 0)
            self._vars[name] = val - 1
            return val

        logger.warning("Cannot evaluate expression: %r", expr)
        return 0

    # ── Built-in Functions ─────────────────────────────────────

    def _call_func(self, name: str, args: list):
        """Dispatch a built-in function call."""
        # Evaluate all arguments
        evaled = [self._eval_expr(a) for a in args]

        if name == "type":
            text = str(evaled[0]) if evaled else ""
            self.hid.type_string(text, stop_event=self._stop)

        elif name == "press":
            combo = str(evaled[0]) if evaled else ""
            self.hid.press_combo(combo)

        elif name == "delay":
            ms = int(evaled[0]) if evaled else 0
            if ms > 0:
                self._stop.wait(ms / 1000.0)

        elif name == "layout":
            name_val = str(evaled[0]) if evaled else self._default_layout
            self.hid.set_layout(name_val)

        elif name == "typingSpeed":
            min_ms = int(evaled[0]) if len(evaled) > 0 else 0
            max_ms = int(evaled[1]) if len(evaled) > 1 else min_ms
            self.hid.set_typing_speed(min_ms, max_ms)

        elif name == "move":
            x = int(evaled[0]) if len(evaled) > 0 else 0
            y = int(evaled[1]) if len(evaled) > 1 else 0
            self.hid.mouse_move(x, y)

        elif name == "moveTo":
            x = int(evaled[0]) if len(evaled) > 0 else 0
            y = int(evaled[1]) if len(evaled) > 1 else 0
            self.hid.mouse_move_stepped(x, y, step=5)

        elif name == "moveStepped":
            x = int(evaled[0]) if len(evaled) > 0 else 0
            y = int(evaled[1]) if len(evaled) > 1 else 0
            step = int(evaled[2]) if len(evaled) > 2 else 10
            self.hid.mouse_move_stepped(x, y, step=step)

        elif name == "click":
            btn = int(evaled[0]) if evaled else BT1
            self.hid.mouse_click(btn)

        elif name == "doubleClick":
            btn = int(evaled[0]) if evaled else BT1
            self.hid.mouse_double_click(btn)

        elif name == "button":
            mask = int(evaled[0]) if evaled else 0
            self.hid.send_mouse_report(mask, 0, 0)

        elif name == "waitLED":
            mask = int(evaled[0]) if evaled else ANY
            timeout = float(evaled[1]) / 1000 if len(evaled) > 1 else 0
            return self.hid.wait_led(mask, self._stop, timeout)

        elif name == "waitLEDRepeat":
            mask = int(evaled[0]) if evaled else ANY
            count = int(evaled[1]) if len(evaled) > 1 else 1
            return self.hid.wait_led_repeat(mask, count, self._stop)

        elif name == "console.log" or name == "log":
            msg = " ".join(str(a) for a in evaled)
            self._output.append(msg)
            logger.debug("[HIDScript] %s", msg)

        elif name in ("parseInt", "Number"):
            try:
                return int(float(evaled[0])) if evaled else 0
            except (ValueError, TypeError):
                return 0

        elif name == "String":
            return str(evaled[0]) if evaled else ""

        elif name == "Math.random":
            import random
            return random.random()

        elif name == "Math.floor":
            import math
            return math.floor(evaled[0]) if evaled else 0

        else:
            logger.warning("Unknown function: %s", name)
            return None

        return None

    # ── Helpers ────────────────────────────────────────────────

    def _parse_args(self, args_str: str) -> list:
        """Split function arguments respecting string literals and parens."""
        args = []
        depth = 0
        current = ""
        in_str = None

        for ch in args_str:
            if in_str:
                current += ch
                if ch == in_str and (len(current) < 2 or current[-2] != '\\'):
                    in_str = None
            elif ch in ('"', "'"):
                in_str = ch
                current += ch
            elif ch == '(':
                depth += 1
                current += ch
            elif ch == ')':
                depth -= 1
                current += ch
            elif ch == ',' and depth == 0:
                if current.strip():
                    args.append(current.strip())
                current = ""
            else:
                current += ch

        if current.strip():
            args.append(current.strip())
        return args

    def _unescape(self, s: str) -> str:
        """Process escape sequences in a string."""
        return s.replace('\\n', '\n').replace('\\t', '\t').replace('\\r', '\r') \
                .replace('\\"', '"').replace("\\'", "'").replace('\\\\', '\\')

    def _skip_ws(self, source: str, pos: int) -> int:
        """Skip whitespace."""
        while pos < len(source) and source[pos] in ' \t\n\r':
            pos += 1
        return pos

    def _find_matching_brace(self, source: str, pos: int) -> int:
        """Find matching } for { at pos."""
        depth = 1
        i = pos + 1
        in_str = None
        while i < len(source):
            ch = source[i]
            if in_str:
                if ch == in_str and source[i-1] != '\\':
                    in_str = None
            elif ch in ('"', "'"):
                in_str = ch
            elif ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    return i
            i += 1
        return len(source) - 1

    def _find_matching_paren(self, source: str, pos: int) -> int:
        """Find matching ) for ( at pos."""
        depth = 1
        i = pos + 1
        in_str = None
        while i < len(source):
            ch = source[i]
            if in_str:
                if ch == in_str and source[i-1] != '\\':
                    in_str = None
            elif ch in ('"', "'"):
                in_str = ch
            elif ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
                if depth == 0:
                    return i
            i += 1
        return len(source) - 1

    def _has_top_level_op(self, expr: str, op: str) -> bool:
        """Check if operator exists at top level (not inside parens/strings)."""
        depth = 0
        in_str = None
        i = 0
        while i < len(expr):
            ch = expr[i]
            if in_str:
                if ch == in_str and (i == 0 or expr[i-1] != '\\'):
                    in_str = None
            elif ch in ('"', "'"):
                in_str = ch
            elif ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
            elif depth == 0 and expr[i:i+len(op)] == op:
                # Don't match multi-char ops that are substrings of longer ones
                if len(op) == 1 and op in '+-':
                    # Skip if part of ++ or --
                    if i + 1 < len(expr) and expr[i+1] == op:
                        i += 2
                        continue
                    if i > 0 and expr[i-1] == op:
                        i += 1
                        continue
                return True
            i += 1
        return False

    def _split_top_level(self, expr: str, op: str, max_splits: int = -1) -> list:
        """Split expression by operator at top level only."""
        parts = []
        depth = 0
        in_str = None
        current = ""
        i = 0
        splits = 0

        while i < len(expr):
            ch = expr[i]
            if in_str:
                current += ch
                if ch == in_str and (i == 0 or expr[i-1] != '\\'):
                    in_str = None
            elif ch in ('"', "'"):
                in_str = ch
                current += ch
            elif ch == '(':
                depth += 1
                current += ch
            elif ch == ')':
                depth -= 1
                current += ch
            elif depth == 0 and expr[i:i+len(op)] == op and (max_splits < 0 or splits < max_splits):
                # Don't split on ++ or -- when looking for + or -
                if len(op) == 1 and op in '+-':
                    if i + 1 < len(expr) and expr[i+1] == op:
                        current += ch
                        i += 1
                        current += expr[i]
                        i += 1
                        continue
                parts.append(current)
                current = ""
                i += len(op)
                splits += 1
                continue
            else:
                current += ch
            i += 1

        parts.append(current)
        return parts

    def _contains_string(self, expr: str) -> bool:
        """Check if expression contains a string literal at top level."""
        depth = 0
        in_str = None
        for ch in expr:
            if in_str:
                if ch == in_str:
                    return True  # Found complete string
            elif ch in ('"', "'"):
                in_str = ch
            elif ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
        return False
