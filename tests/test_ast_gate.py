import pytest

from crawloop.safety import ASTViolation, ast_check

GOOD = '''
from parsel import Selector
import re
from crawloop.contract import Crawler, CrawlResult, FetchContext
from urllib.parse import urljoin
class C(Crawler):
    family = "x"; schema_ref = "Product@1"
    async def crawl(self, url, ctx):
        sel = Selector(await ctx.fetch(url))
        return CrawlResult(items=[{"name": sel.css("h1::text").get()}])
'''

# A second GOOD sample exercising every allowed import + helper usage,
# to make sure the gate is not over-broad.
GOOD_FULL = '''
import re
import json
import datetime
import decimal
from urllib.parse import urljoin, urlparse
from parsel import Selector
from crawloop.contract import Crawler, CrawlResult


class C2(Crawler):
    family = "y"
    schema_ref = "Quote@1"

    async def crawl(self, url, ctx):
        html = await ctx.fetch(url)
        sel = Selector(html)
        title = sel.css("h1::text").get()
        return CrawlResult(items=[{"title": title, "scraped_at": str(datetime.date.today())}])
'''

BAD = {
    "import_os": "import os",
    "import_subprocess": "import subprocess",
    "import_httpx": "import httpx",
    "import_requests": "import requests",
    "dunder_import": "x = __import__('os')",
    "eval": "y = eval('1+1')",
    "exec": "exec('x=1')",
    "compile": "compile('1','<s>','eval')",
    "open_file": "f = open('/etc/passwd')",
    "getattr_str": "getattr(obj, 'system')",
    "dunder_attr": "x = ().__class__.__bases__",
    "from_os_import": "from os import system",
    # additional vectors from the task spec (all must be rejected)
    "import_alias": "import os as o",
    "from_sub_import": "from os.path import join",
    "import_crawler_registry": "import crawloop.registry",
    "subclasses": "x = object.__subclasses__()",
    "globals_call": "g = globals()",
    "builtins_breakpoint": "breakpoint()",
    "input_call": "input()",
    "dunder_globals_attr": "f = (lambda: 0).__globals__",
    # extra bypass vectors I added during self-review
    "import_sys": "import sys",
    "import_socket": "import socket",
    "import_pathlib": "import pathlib",
    "import_importlib": "import importlib",
    "import_builtins": "import builtins",
    "import_ctypes": "import ctypes",
    "import_pickle": "import pickle",
    "import_crawler_safety": "import crawloop.safety",
    "import_crawler_bare": "import crawloop",
    # urllib egress hole: bare urllib (and thus urllib.request) must be blocked;
    # only urllib.parse is allowed. urllib.request.urlopen would bypass FetchContext.
    "import_urllib_bare": "import urllib",
    "import_urllib_request": "import urllib.request",
    "from_urllib_request": "from urllib.request import urlopen",
    "from_dotdot": "from .. import x",
    "from_dot": "from . import os",
    "from_dot_contract": "from .contract import Crawler",
    "from_star_os": "from os import *",
    "setattr_call": "setattr(o, 'a', 1)",
    "delattr_call": "delattr(o, 'a')",
    "locals_call": "locals()",
    "vars_call": "vars()",
    "memoryview_call": "memoryview(b'')",
    "build_class": "__build_class__(c, 'n')",
    "bare_dunder_import_name": "f = __import__",
    "dunder_dict": "d = obj.__dict__",
    "dunder_mro": "m = type(obj).__mro__",
    "dunder_subclasshook": "h = obj.__subclasshook__",
    "getattr_in_expr": "v = getattr(o, 'x') + 1",
    # bare-reference bypasses of the banned-call rule (alias / walrus / decorator)
    "alias_eval": "e = eval\ne('1')",
    "walrus_exec": "(e := exec)('x=1')",
    "decorator_eval": "@eval\ndef f():\n    pass",
    "bare_open_name": "fn = open",
    "bare_getattr_name": "g = getattr",
    # __builtins__ exposes the entire builtins namespace -> reach any builtin
    "builtins_subscript": "__builtins__['eval']('1')",
    "builtins_get": "__builtins__.get('eval')",
    "bare_builtins_name": "b = __builtins__",
    # bare dunder NAMES: __loader__/__spec__ are the live import machinery at
    # module-load time -> in-process arbitrary code execution / file read.
    "loader_rce": (
        "FunctionType = type(lambda: 0)\n"
        'code = __loader__.source_to_code(b"import os", "<x>")\n'
        "FunctionType(code, {})()\n"
    ),
    "loader_name": "ldr = __loader__",
    "loader_getdata": "data = __loader__.get_data('/etc/passwd')",
    "spec_name": "s = __spec__.loader",
    "file_leak": "p = __file__",
    "decorator_loader": "@__loader__\ndef f():\n    pass",
    # str.format / format_map reach attributes via the runtime format mini-language
    "str_format_escape": 'v = "{0.__class__}".format(json)',
    "format_map_escape": 'v = "{x.__class__}".format_map({"x": json})',
}


def test_good_passes():
    assert ast_check(GOOD) == []


def test_good_full_passes():
    assert ast_check(GOOD_FULL) == []


@pytest.mark.parametrize("name,src", list(BAD.items()))
def test_bad_rejected(name, src):
    assert ast_check(src), f"{name} should be rejected"


def test_check_or_raise():
    with pytest.raises(ASTViolation):
        ast_check("import os", raise_on_violation=True)


def test_no_raise_when_clean():
    # raise_on_violation must NOT raise for clean source.
    assert ast_check(GOOD, raise_on_violation=True) == []


def test_syntax_error_is_violation():
    assert ast_check("def (:")  # unparseable -> treated as a violation, not a crash


def test_syntax_error_raises_when_requested():
    with pytest.raises(ASTViolation):
        ast_check("def (:", raise_on_violation=True)


def test_violation_messages_include_lineno():
    violations = ast_check("import os")
    assert violations
    assert any("line" in v for v in violations)
