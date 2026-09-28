# Contributing

Changes should be small, tested and safe for untrusted disk images. Do not add
fixtures copied from private or copyrighted media; generate the smallest bytes
needed by the test instead.

Before opening a pull request, run:

```shell
make check
```

Code must pass Ruff formatting and linting, mypy, and pytest. New parser paths
need tests for malformed, truncated and boundary-value input. Filesystem writes
must not be added without an explicit recovery design and failure-injection
tests.

Contributions are accepted under the project's MIT licence. By submitting a
change, contributors confirm that they have the right to provide it under those
terms.
