# mutate4swift

`mutate4swift` performs source-level mutation testing for Swift projects. It changes one operator or Boolean value at a time, runs the test command, and restores the original source in a `finally` block.

## Install

```bash
pipx install git+https://github.com/lukasa1993/mutate4swift.git
```

## Run

```bash
mutate4swift --test-command "swift test --enable-code-coverage"
```

The command runs the unchanged test suite first. It stops if the baseline fails. Use `--skip-baseline` only when another step already verified the same source.

Useful options:

```bash
mutate4swift --list
mutate4swift --max-mutants 25
mutate4swift --json
```

Results are written to `target/mutation/mutate4swift.json`. Exit status `2` means that one or more mutants survived.

## Development

```bash
python -m pip install -e . pytest
pytest -q
```
