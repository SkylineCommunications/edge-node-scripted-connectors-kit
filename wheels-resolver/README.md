# Wheels Resolver

Downloads Python 3.14 wheels for **Windows** (`x86_64-windows-msvc`) and **Linux** (`x86_64-linux-gnu`) from a `requirements.txt` file and organises them into platform-specific directories.

**Requires Python 3.10+**

---

## Output structure

```
<output>/
└── dependencies/
    ├── x86_64-windows-msvc/   # Windows-only wheels
    ├── x86_64-linux-gnu/      # Linux-only wheels
    └── universal/             # Shared / pure-Python wheels
```

> [!WARNING]
> If `<output>/dependencies/` already exists it will be **deleted and recreated**.

---

## Modes

### `download` — resolve and download wheels from a requirements file

```
python wheels_resolver.py download -r <requirements> -o <output> [-v] [-l <logfile>]
```

| Flag | Required | Description |
|------|----------|-------------|
| `-r`, `--requirements` | ✅ | Path to `requirements.txt` / `requirements.in` |
| `-o`, `--output` | ✅ | Output directory |
| `-v`, `--verbose` | ❌ | Enable debug logging |
| `-l`, `--log-file` | ❌ | Log file path (default: `wheels_resolver.log`) |

**Example:**

```bash
python wheels_resolver.py download -r requirements.txt -o ./output
```

---

### `resolve` — check wheel availability for a single package

```
python wheels_resolver.py resolve -P <package> [-v] [-l <logfile>]
```

| Flag | Required | Description |
|------|----------|-------------|
| `-P`, `--package` | ✅ | Package spec, e.g. `requests` or `requests==2.33.1` |
| `-v`, `--verbose` | ❌ | Enable debug logging |
| `-l`, `--log-file` | ❌ | Log file path (default: `wheels_resolver.log`) |

**Examples:**

```bash
# Check if wheels exist for a package
python wheels_resolver.py resolve -P requests

# Check a pinned version
python wheels_resolver.py resolve -P "requests==2.33.1"
```

---

## Prerequisites

`pip` should be installed in the Python interpreter used to run `wheels_resolver.py`, or available in your `PATH`.

---

## Exit codes

| Code | Meaning |
|------|---------|
| `0` | Success |
| `1` | pip not found |
| `2` | Conflicting dependencies |
| `3` | Requirements file not found |
| `4` | Python version is lower than 3.10 |
| `5` | Invalid mode |
| `99` | Unexpected error |
