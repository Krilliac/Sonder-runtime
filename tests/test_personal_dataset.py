import build_personal_dataset as bpd

FS = "\x1f"
RS = "\x1e"


def _log(records):
    """Build synthetic git-log text from a list of (hash, subject, body)."""
    out = []
    for h, s, b in records:
        out.append(h + FS + s + FS + b + RS + "\n")
    return "".join(out)


def test_parse_git_log_two_records():
    text = _log([
        ("abc123", "fix widget leak", "The widget leaked handles on close.\nFixed by releasing in dtor."),
        ("def456", "typo", ""),
    ])
    commits = bpd.parse_git_log(text)
    assert len(commits) == 2
    assert commits[0] == {
        "hash": "abc123",
        "subject": "fix widget leak",
        "body": "The widget leaked handles on close.\nFixed by releasing in dtor.",
    }
    assert commits[1]["hash"] == "def456"
    assert commits[1]["subject"] == "typo"
    assert commits[1]["body"] == ""


def test_parse_git_log_empty_input():
    assert bpd.parse_git_log("") == []


def test_parse_git_log_trailing_separator():
    text = "abc" + FS + "subj" + FS + "body text here" + RS
    commits = bpd.parse_git_log(text)
    assert len(commits) == 1
    assert commits[0]["hash"] == "abc"


def test_commit_pairs_skips_thin_body_keeps_rich_body():
    commits = [
        {"hash": "1", "subject": "small fix", "body": "typo"},
        {"hash": "2", "subject": "big refactor",
         "body": "Refactored the connection pool to avoid a deadlock under load. "
                  "This was causing production outages during peak traffic."},
    ]
    pairs = bpd.commit_pairs(commits, "myproj")
    assert len(pairs) == 1
    msg = pairs[0]["messages"]
    assert msg[0]["role"] == "user"
    assert "myproj" in msg[0]["content"]
    assert "big refactor" in msg[0]["content"]
    assert msg[1]["role"] == "assistant"
    assert "deadlock" in msg[1]["content"]


def test_commit_pairs_empty_input_no_crash():
    assert bpd.commit_pairs([], "myproj") == []


CPP_SAMPLE = "/** Adds two ints. */\nint add(int a,int b){return a+b;}\n"


def test_extract_units_cpp_block_doc():
    units = bpd.extract_units(CPP_SAMPLE, "cpp")
    assert len(units) == 1
    u = units[0]
    assert u["name"] == "add"
    assert "Adds two ints" in u["doc"]
    assert "return a+b" in u["body"]
    assert "add" in u["signature"]


CPP_LINE_DOC_SAMPLE = (
    "// Computes the maximum of two values.\n"
    "// Ties go to the first argument.\n"
    "int Widget::maxOf(int a, int b) {\n"
    "    if (a >= b) { return a; }\n"
    "    return b;\n"
    "}\n"
)


def test_extract_units_cpp_line_doc_and_qualified_name():
    units = bpd.extract_units(CPP_LINE_DOC_SAMPLE, "cpp")
    assert len(units) == 1
    u = units[0]
    assert u["name"] == "Widget::maxOf"
    assert "maximum" in u["doc"]
    assert "Ties go to the first argument" in u["doc"]
    assert "return b;" in u["body"]


PY_SAMPLE = (
    "def foo(x):\n"
    "    \"\"\"Return x doubled.\"\"\"\n"
    "    return x*2\n"
)


def test_extract_units_python_docstring():
    units = bpd.extract_units(PY_SAMPLE, "py")
    assert len(units) == 1
    u = units[0]
    assert u["name"] == "foo"
    assert "Return x doubled" in u["doc"]
    assert "return x*2" in u["body"]
    assert u["signature"] == "def foo(x):"


PY_MULTILINE_DOC_SAMPLE = (
    "class Thing:\n"
    "    def bar(self, y):\n"
    "        '''\n"
    "        Do a thing with y.\n"
    "        Returns the result.\n"
    "        '''\n"
    "        z = y + 1\n"
    "        return z\n"
    "\n"
    "    def undocumented(self):\n"
    "        return None\n"
)


def test_extract_units_python_multiline_docstring_and_skips_undocumented():
    units = bpd.extract_units(PY_MULTILINE_DOC_SAMPLE, "py")
    names = [u["name"] for u in units]
    assert "bar" in names
    assert "undocumented" not in names
    bar = next(u for u in units if u["name"] == "bar")
    assert "Do a thing with y" in bar["doc"]
    assert "z = y + 1" in bar["body"]
    assert "return z" in bar["body"]


CS_SAMPLE = (
    "/// Adds two numbers together.\n"
    "public int Add(int a, int b) { return a + b; }\n"
)


def test_extract_units_cs_xml_doc():
    units = bpd.extract_units(CS_SAMPLE, "cs")
    assert len(units) == 1
    u = units[0]
    assert u["name"] == "Add"
    assert "Adds two numbers together" in u["doc"]
    assert "return a + b;" in u["body"]


def test_extract_units_no_doc_source_no_crash():
    source = "int add(int a, int b) { return a + b; }\n"
    assert bpd.extract_units(source, "cpp") == []


def test_extract_units_empty_source_no_crash():
    assert bpd.extract_units("", "cpp") == []
    assert bpd.extract_units("", "py") == []
    assert bpd.extract_units("", "cs") == []


def test_doc_pairs_and_impl_pairs_shape():
    units = bpd.extract_units(CPP_SAMPLE, "cpp")
    dpairs = bpd.doc_pairs(units, "myproj", "cpp")
    ipairs = bpd.impl_pairs(units, "myproj", "cpp")

    assert len(dpairs) == 1
    dm = dpairs[0]["messages"]
    assert dm[0]["role"] == "user"
    assert "myproj" in dm[0]["content"]
    assert "add" in dm[0]["content"]
    assert dm[1]["role"] == "assistant"
    assert "Adds two ints" in dm[1]["content"]

    assert len(ipairs) == 1
    im = ipairs[0]["messages"]
    assert im[0]["role"] == "user"
    assert "add" in im[0]["content"]
    assert im[1]["role"] == "assistant"
    assert im[1]["content"].startswith("```cpp\n")
    assert im[1]["content"].endswith("```")
    assert "return a+b" in im[1]["content"]


def test_doc_pairs_skips_units_without_doc():
    units = [{"name": "f", "signature": "int f()", "doc": "", "body": "int f(){return 0;}"}]
    assert bpd.doc_pairs(units, "p", "cpp") == []


def test_impl_pairs_skips_units_without_body():
    units = [{"name": "f", "signature": "int f()", "doc": "does f", "body": ""}]
    assert bpd.impl_pairs(units, "p", "cpp") == []


def test_no_pairs_no_crash_on_empty_units():
    assert bpd.doc_pairs([], "p", "cpp") == []
    assert bpd.impl_pairs([], "p", "cpp") == []
