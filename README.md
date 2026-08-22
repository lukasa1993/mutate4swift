# mutate4swift

`mutate4swift` performs syntax-aware mutation testing for Swift. It verifies the baseline, restores source through a crash-recovery journal, separates timeouts and compile errors from killed mutants, and writes a versioned JSON manifest.

```bash
pipx install git+https://github.com/lukasa1993/mutate4swift.git
mutate4swift --test-command "<project test command>" --validate-command "<project build command>"
```

Compiled C-family projects require a detected or explicit validation command. Timeouts, invalid syntax, and compile errors return status `1`; surviving mutants return status `2`.

## Development

```bash
python -m pip install -e . pytest
pytest -q
```
