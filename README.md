# mutate4swift

`mutate4swift` performs syntax-aware Swift mutation testing. It verifies the baseline, validates each mutant with SwiftPM, isolates timeouts, and restores source through an atomic crash-recovery journal.

```bash
pipx install git+https://github.com/lukasa1993/mutate4swift.git
mutate4swift --test-command "swift test --quiet" --validate-command "swift build --quiet"
```

Timeout, invalid, and compile-error mutants never count as killed. Exit status: `0` pass, `1` infrastructure or invalid-mutant failure, `2` surviving mutant.
