import timeit


def f(q, /, *params, into=None, strategy="auto"):  # noqa: ANN001, ANN201
    return params


g = {"f": f, "q": "q", "p": (1,), "i": int, "s": "direct"}
for label, stmt in [
    ("f(q, *p)", "f(q, *p)"),
    ("f(q, p[0])", "f(q, p[0])"),
    ("f(q, *p, into=i, strategy=s)", "f(q, *p, into=i, strategy=s)"),
    ("f(q, p[0], into=i, strategy=s)", "f(q, p[0], into=i, strategy=s)"),
    ("f(q, 1, into=i, strategy=s)", "f(q, 1, into=i, strategy=s)"),
]:
    n = 2_000_000
    best = min(timeit.repeat(stmt, globals=g, number=n, repeat=7))
    print(f"{label:34s} {best / n * 1e9:6.1f} ns")
