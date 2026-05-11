# Contributing

Thank you for improving this project.

- **Issues & PRs**: Describe the problem or change clearly; link related discussions when helpful.
- **Style**: Match existing code layout and naming; keep changes focused on the stated goal.
- **Checks**: Before opening a PR, run locally:
  - `cd src && KB_DATA_DIR=../data python3 kb.py validate`
  - `python3 -m py_compile src/kb.py src/kb-server.py src/kb_tz.py`
- **Tracked junk**: If `.DS_Store` or `__pycache__` were committed before `.gitignore`, remove from the index only:
  - `git rm --cached .DS_Store` (and other paths as needed)
  - `git rm -r --cached src/__pycache__`

Licensed under the MIT License (see `LICENSE`).
