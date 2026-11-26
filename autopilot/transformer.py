"""Wraps a function changed by a PR so both versions ship behind a flag.

Given the file on the base branch (old) and in the PR (new), and the name of
a changed top-level function `f`, wrap() returns the PR's file with `f`
replaced by:

    def _f__old(<same signature>):   # base branch body
    def _f__new(<same signature>):   # PR body
    @<f's decorators>
    def f(<same signature>):         # dispatcher; the rest of the app still calls f
        if flag_on("<flag>"):
            return _f__new(<args>)
        return _f__old(<args>)

The dispatcher keeps f's exact signature (not *args, **kwargs), so frameworks
that read parameters, like FastAPI, still work. Decorators stay on the
dispatcher only, so a route is registered once.

No AI here: Python's own parser (ast) finds the functions, and every case it
can't wrap safely raises TransformError with a reason for the PR comment.
Everything outside f comes from the PR unchanged.
"""

import ast
import re

SDK_IMPORT = "from autopilot_sdk import flag_on"
FLAG_NAME = re.compile(r"^ap_[a-z0-9_]{1,60}$")


class TransformError(Exception):
    """The function can't be wrapped safely; the message says why."""


def _parse(source, label):
    try:
        return ast.parse(source)
    except SyntaxError as e:
        raise TransformError(f"the {label} version of the file isn't valid Python (line {e.lineno})") from e


def _top_level_functions(tree):
    return {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _defined_anywhere(tree, name):
    """True if `name` is a function or class defined at any depth (methods included)."""
    return any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == name
               for n in ast.walk(tree))


def _shape(node):
    """The function minus its name, decorators and positions, to compare versions."""
    clone = ast.parse(ast.unparse(node)).body[0]
    clone.name, clone.decorator_list = "_", []
    return ast.dump(clone)


def changed_functions(base_source, head_source):
    """Top-level functions that exist in both versions and differ, as a sorted list.
    Used to check the AI's choice of function against what the PR really changed."""
    base = _top_level_functions(_parse(base_source, "base"))
    head = _top_level_functions(_parse(head_source, "PR"))
    return sorted(name for name in base.keys() & head.keys() if _shape(base[name]) != _shape(head[name]))


def _is_async_generator(node):
    """An `async def` that yields can't be awaited, so the dispatcher can't call it."""
    if not isinstance(node, ast.AsyncFunctionDef):
        return False
    stack = list(node.body)
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.Yield, ast.YieldFrom)):
            return True
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            stack.extend(ast.iter_child_nodes(n))
    return False


def _signature(node):
    returns = f" -> {ast.unparse(node.returns)}" if node.returns else ""
    return f"({ast.unparse(node.args)}){returns}"


def _call_arguments(args):
    """The argument list that passes every parameter of `args` through unchanged."""
    parts = [a.arg for a in args.posonlyargs + args.args]
    if args.vararg:
        parts.append(f"*{args.vararg.arg}")
    parts += [f"{a.arg}={a.arg}" for a in args.kwonlyargs]
    if args.kwarg:
        parts.append(f"**{args.kwarg.arg}")
    return ", ".join(parts)


def _function_text(source_lines, node, new_name):
    """The function's source from its `def` line to its end (decorators dropped),
    renamed to `new_name`. Comments inside the body are kept."""
    lines = source_lines[node.lineno - 1:node.end_lineno]
    pattern = re.compile(rf"^(\s*(?:async\s+)?def\s+){re.escape(node.name)}\b")
    if not pattern.match(lines[0]):
        raise TransformError(f"couldn't find the `def {node.name}` line")
    lines[0] = pattern.sub(rf"\g<1>{new_name}", lines[0], count=1)
    return "\n".join(lines)


def _insert_import(source):
    """Add the flag_on import after the module docstring, __future__ imports and
    other top-level imports, unless the file already has it."""
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "autopilot_sdk" \
                and any(alias.name == "flag_on" and alias.asname is None for alias in node.names):
            return source
    insert_after, last = 0, None  # line after which to insert (0 = top of file), and what's there
    for i, node in enumerate(tree.body):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            insert_after, last = node.end_lineno, "import"
        elif (i == 0 and isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
              and isinstance(node.value.value, str)):
            insert_after, last = node.end_lineno, "docstring"
        else:
            break
    if last is None:
        block = [SDK_IMPORT, ""]      # top of file, blank line before the code
    elif last == "docstring":
        block = ["", SDK_IMPORT]      # blank line between docstring and import
    else:
        block = [SDK_IMPORT]          # joins the existing import block
    lines = source.split("\n")
    return "\n".join(lines[:insert_after] + block + lines[insert_after:])


def wrap(base_source, head_source, func_name, flag_name):
    """Return the PR's file with `func_name` wrapped behind `flag_name`.
    Raises TransformError if it can't be done safely."""
    if not FLAG_NAME.match(flag_name or ""):
        raise TransformError(f"flag name {flag_name!r} must look like ap_lower_snake_case")
    if base_source is None:
        raise TransformError("the file is new in this PR, so there's no old version to fall back to")
    # Files committed from Windows can have CRLF endings; work in LF and give back what came in.
    crlf = "\r\n" in head_source
    base_source, head_source = base_source.replace("\r\n", "\n"), head_source.replace("\r\n", "\n")
    result = _wrap(base_source, head_source, func_name, flag_name)
    return result.replace("\n", "\r\n") if crlf else result


def _wrap(base_source, head_source, func_name, flag_name):

    base_tree, head_tree = _parse(base_source, "base"), _parse(head_source, "PR")
    base_fns, head_fns = _top_level_functions(base_tree), _top_level_functions(head_tree)
    old_name, new_name = f"_{func_name}__old", f"_{func_name}__new"

    if func_name not in head_fns:
        if _defined_anywhere(head_tree, func_name):
            raise TransformError(f"`{func_name}` isn't a top-level function (methods and nested "
                                 "functions can't be wrapped automatically)")
        raise TransformError(f"`{func_name}` isn't defined in the PR's version of the file")
    if func_name not in base_fns:
        raise TransformError(f"`{func_name}` is new in this PR, so there's no old version to fall back to")
    for taken in (old_name, new_name):
        if _defined_anywhere(head_tree, taken) or _defined_anywhere(base_tree, taken):
            raise TransformError(f"`{taken}` already exists; this function looks already wrapped")

    for label, tree in (("base", base_tree), ("PR", head_tree)):
        if sum(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == func_name for n in tree.body) > 1:
            raise TransformError(f"`{func_name}` is defined more than once in the {label} version of the file")

    old, new = base_fns[func_name], head_fns[func_name]
    if _is_async_generator(old) or _is_async_generator(new):
        raise TransformError(f"`{func_name}` is an async generator, which can't be wrapped automatically")
    if _shape(old) == _shape(new):
        if [ast.dump(d) for d in old.decorator_list] != [ast.dump(d) for d in new.decorator_list]:
            raise TransformError(f"only `{func_name}`'s decorators changed; a decorator change "
                                 "can't be switched per user, so it can't go behind a flag")
        raise TransformError(f"`{func_name}` didn't change in this PR")
    if isinstance(old, ast.AsyncFunctionDef) != isinstance(new, ast.AsyncFunctionDef):
        raise TransformError(f"`{func_name}` changed between `def` and `async def`; callers would break")
    if ast.dump(old.args) != ast.dump(new.args):
        raise TransformError(f"`{func_name}`'s parameters changed ({_signature(old)} -> {_signature(new)}); "
                             "the old version couldn't accept the new calls, so it can't be a fallback")

    head_lines = head_source.split("\n")
    base_lines = base_source.split("\n")
    first_line = min([new.lineno] + [d.lineno for d in new.decorator_list])
    decorators = head_lines[first_line - 1:new.lineno - 1]

    is_async = isinstance(new, ast.AsyncFunctionDef)
    call = _call_arguments(new.args)
    awaiting = "await " if is_async else ""
    dispatcher = decorators + [
        f"{'async ' if is_async else ''}def {func_name}{_signature(new)}:",
        f'    """Release Autopilot: runs the new version for users the flag `{flag_name}` is on for."""',
        f'    if flag_on("{flag_name}"):',
        f"        return {awaiting}{new_name}({call})",
        f"    return {awaiting}{old_name}({call})",
    ]
    replacement = "\n".join([
        _function_text(base_lines, old, old_name), "", "",
        _function_text(head_lines, new, new_name), "", "",
        "\n".join(dispatcher),
    ])
    result = "\n".join(head_lines[:first_line - 1] + [replacement] + head_lines[new.end_lineno:])
    result = _insert_import(result)
    _check(result, base_source, head_source, func_name, old_name, new_name, flag_name)
    return result


def _check(result, base_source, head_source, func_name, old_name, new_name, flag_name):
    """Prove the output is what we claim before anyone commits it."""
    try:
        tree = ast.parse(result)
        compile(result, "<wrapped>", "exec")
    except SyntaxError as e:
        raise TransformError(f"internal error: the wrapped file isn't valid Python (line {e.lineno})") from e
    fns = _top_level_functions(tree)
    base_fn = _top_level_functions(ast.parse(base_source))[func_name]
    head_fn = _top_level_functions(ast.parse(head_source))[func_name]
    if old_name not in fns or _shape(fns[old_name]) != _shape(base_fn):
        raise TransformError("internal error: the old version wasn't copied exactly")
    if new_name not in fns or _shape(fns[new_name]) != _shape(head_fn):
        raise TransformError("internal error: the new version wasn't copied exactly")
    if func_name not in fns or not _asks_flag(fns[func_name], flag_name):
        raise TransformError("internal error: the dispatcher is missing")
    # Everything else in the file must be exactly the PR's version.
    others = lambda t, skip: [ast.dump(n) for n in t.body  # noqa: E731
                              if not (isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in skip)
                              and not (isinstance(n, ast.ImportFrom) and n.module == "autopilot_sdk")]
    if others(tree, {func_name, old_name, new_name}) != others(ast.parse(head_source), {func_name}):
        raise TransformError("internal error: code outside the function changed")


def _asks_flag(node, flag_name):
    return any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "flag_on"
               and len(n.args) == 1 and isinstance(n.args[0], ast.Constant) and n.args[0].value == flag_name
               for n in ast.walk(node))


def wrapper_parts(source, func_name, flag_name):
    """The parts of a wrapped file that must stay exactly as approved: the old
    version (the fallback) and the dispatcher (the switch). Compared on merge, so
    commits pushed after approval can change the new version but never weaken
    the fallback or the switch. Raises TransformError if the wrapper is gone."""
    fns = _top_level_functions(_parse(source, "merged"))
    old_name, new_name = f"_{func_name}__old", f"_{func_name}__new"
    missing = [n for n in (old_name, new_name, func_name) if n not in fns]
    if missing:
        raise TransformError(f"`{'`, `'.join(missing)}` missing: the flag wrapper isn't there any more")
    if not _asks_flag(fns[func_name], flag_name):
        raise TransformError(f"`{func_name}` no longer asks the flag `{flag_name}`")
    dispatcher = ast.dump(ast.parse(ast.unparse(fns[func_name])).body[0])
    return {"old": _shape(fns[old_name]), "dispatcher": dispatcher}


def function_source(source, name):
    """Source text of a top-level function (for the diagnosis prompt), or None."""
    try:
        node = _top_level_functions(ast.parse(source)).get(name)
    except SyntaxError:
        return None
    return ast.get_source_segment(source, node) if node else None


def unwrap(source, func_name, flag_name):
    """The reverse of wrap(), for the cleanup PR: keep the new version as `f`
    (with the dispatcher's decorators), delete the old version and the
    dispatcher, and drop the flag_on import if nothing else uses it.
    unwrap(wrap(base, head, f, flag), f, flag) gives back the PR's file.
    Raises TransformError if the file isn't wrapped with this flag."""
    crlf = "\r\n" in source
    source = source.replace("\r\n", "\n")
    result = _unwrap(source, func_name, flag_name)
    return result.replace("\n", "\r\n") if crlf else result


def _unwrap(source, func_name, flag_name):
    tree = _parse(source, "main")
    old_name, new_name = f"_{func_name}__old", f"_{func_name}__new"
    for name in (old_name, new_name, func_name):
        count = sum(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name for n in tree.body)
        if count != 1:
            raise TransformError(f"expected one top-level `{name}`, found {count}: the file isn't wrapped as expected")
    fns = _top_level_functions(tree)
    old, new, dispatcher = fns[old_name], fns[new_name], fns[func_name]
    if not _asks_flag(dispatcher, flag_name):
        raise TransformError(f"`{func_name}` isn't switched by `{flag_name}`")
    # Nothing but the dispatcher may use the old version, or deleting it would break the file.
    for node in tree.body:
        if node in (old, dispatcher):
            continue
        if any(isinstance(n, ast.Name) and n.id == old_name for n in ast.walk(node)):
            raise TransformError(f"`{old_name}` is used outside the dispatcher, so it can't be removed")

    lines = source.split("\n")

    def start(node):  # first line, decorators included (0-based)
        return min([node.lineno] + [d.lineno for d in node.decorator_list]) - 1

    drop = set(range(start(old), old.end_lineno))
    i = old.end_lineno
    while i < len(lines) and not lines[i].strip():   # blank lines after the old version
        drop.add(i)
        i += 1
    drop.update(range(start(dispatcher), dispatcher.end_lineno))
    i = start(dispatcher) - 1
    while i >= 0 and not lines[i].strip() and i not in drop:  # blank lines before the dispatcher
        drop.add(i)
        i -= 1
    decorators = lines[start(dispatcher):dispatcher.lineno - 1]
    renamed = decorators + _function_text(lines, new, func_name).split("\n")

    out, i = [], 0
    while i < len(lines):
        if i == start(new):
            out += renamed
            i = new.end_lineno
            continue
        if i not in drop:
            out.append(lines[i])
        i += 1
    result = _drop_unused_import("\n".join(out))
    _check_unwrapped(result, source, func_name, new, dispatcher)
    return result


def _drop_unused_import(source):
    tree = ast.parse(source)
    if any(isinstance(n, ast.Name) and n.id == "flag_on" for n in ast.walk(tree)):
        return source
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "autopilot_sdk" \
                and [(a.name, a.asname) for a in node.names] == [("flag_on", None)]:
            lines = source.split("\n")
            a, b = node.lineno - 1, node.end_lineno
            before_blank = a > 0 and not lines[a - 1].strip()
            after_blank = b < len(lines) and not lines[b].strip()
            if a == 0 and after_blank:
                b += 1                      # top of file: also drop the blank line after it
            elif before_blank and after_blank:
                a -= 1                      # after a docstring: don't leave two blank lines
            return "\n".join(lines[:a] + lines[b:])
    return source


def _check_unwrapped(result, source, func_name, new, dispatcher):
    try:
        tree = ast.parse(result)
        compile(result, "<unwrapped>", "exec")
    except SyntaxError as e:
        raise TransformError(f"internal error: the cleaned-up file isn't valid Python (line {e.lineno})") from e
    fns = _top_level_functions(tree)
    if f"_{func_name}__old" in fns or f"_{func_name}__new" in fns or func_name not in fns:
        raise TransformError("internal error: the cleanup didn't leave exactly one function")
    if _shape(fns[func_name]) != _shape(new):
        raise TransformError("internal error: the kept function isn't the released new version")
    if [ast.dump(d) for d in fns[func_name].decorator_list] != [ast.dump(d) for d in dispatcher.decorator_list]:
        raise TransformError("internal error: the decorators weren't kept")
    skip = {func_name, f"_{func_name}__old", f"_{func_name}__new"}
    others = lambda t: [ast.dump(n) for n in t.body  # noqa: E731
                        if not (isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in skip)
                        and not (isinstance(n, ast.ImportFrom) and n.module == "autopilot_sdk")]
    if others(tree) != others(ast.parse(source)):
        raise TransformError("internal error: code outside the function changed")