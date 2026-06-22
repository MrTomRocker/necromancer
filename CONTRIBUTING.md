# Contributing

Contributing to this project should be as easy and transparent as possible, whether it's:

- Reporting a bug
- Discussing the current state of the code
- Submitting a fix
- Proposing new features

## GitHub is used for everything

GitHub is used to host the code, to track issues and feature requests, and to accept pull requests.

Pull requests are the best way to propose changes to the codebase:

1. Fork the repo and create your branch from `main`.
2. If you've changed behaviour, update the documentation (`README.md` and the relevant page under [`docs/arch/`](./docs/arch)).
3. Make sure your code lints and is formatted (see [Coding style](#coding-style)).
4. Test your contribution (see [Testing](#testing)).
5. Open the pull request.

Please keep changes focused — one logical change per pull request is much easier to review.

## Any contributions you make will be under the MIT Software License

When you submit code changes, your submissions are understood to be under the same [MIT License](./LICENSE) that covers the project. Feel free to contact the maintainer if that's a concern.

## Report bugs using GitHub's [issues](../../issues)

GitHub issues are used to track public bugs. Report a bug by [opening a new issue](../../issues/new/choose) — it's that easy.

## Write bug reports with detail, background, and a way to reproduce

**Great bug reports** tend to have:

- A quick summary and/or background.
- Steps to reproduce — be specific, and include the guard configuration (health source, strategy, behaviour) when relevant.
- The relevant `necromancer` log lines (set the logger to `debug` — see below) and the value of `sensor.<guard>_status`.
- What you expected to happen.
- What actually happened.
- Notes (why you think this might be happening, or things you tried that didn't work).

Enable debug logging by adding to `configuration.yaml`:

```yaml
logger:
  logs:
    custom_components.necromancer: debug
```

## Coding style

The project uses [`ruff`](https://docs.astral.sh/ruff/) for both linting and formatting — the same configuration Home Assistant Core uses. Before opening a pull request:

```bash
ruff check custom_components/necromancer
ruff format custom_components/necromancer
python -m py_compile custom_components/necromancer/*.py
```

Additional house rules:

- **Logging:** lazy `%`-formatting (never f-strings in log calls), no trailing period, no component name prefix (the logger name already carries it).
- **Translations:** custom components ship **no `strings.json`** (a Core build-time file) — `translations/en.json` is the source, edited directly, and `translations/de.json` mirrors its keys. Placeholder sets per key must stay consistent, every flow step needs a `description`, and descriptions must contain **no `{…}` braces** except real `description_placeholders` (HA renders config translations via ICU MessageFormat).

## Testing

See [`docs/arch/testing.md`](./docs/arch/testing.md) for the full test concept. In
short: run the suites under `tests/` (`test_units`, `test_poe`, `test_engine`,
`test_integration`) against a Home Assistant core checkout, plus the pre-commit
gates (ruff check/format, `py_compile`, translation symmetry, `hassfest`).

This custom component is developed inside a Home Assistant Core **dev container** with the package mounted live, so edits take effect without a redeploy. The container ships a standalone Home Assistant instance pre-configured for development.

## License

By contributing, you agree that your contributions will be licensed under the project's [MIT License](./LICENSE).
