# Git Hooks

This directory contains git hooks that are automatically installed during `make init`.

## Pre-commit Hook

The pre-commit hook automatically runs `make black` before every commit to ensure code is properly formatted.

### How it works:

1. When you run `git commit`, the hook runs `make black` automatically
2. If no formatting changes are needed, the commit proceeds normally
3. If black makes formatting changes:
   - The changes are automatically staged
   - The commit proceeds with the formatted code
   - No need to commit again

### Installation:

The hook is automatically installed when you run:
```bash
make init
```

This copies `.git-hooks/pre-commit` to `.git/hooks/pre-commit` and makes it executable.

### Manual Installation:

If you need to reinstall the hook manually:
```bash
cp .git-hooks/pre-commit .git/hooks/pre-commit
chmod +x .git/hooks/pre-commit
```

### Bypassing the Hook:

If you need to bypass the pre-commit hook (not recommended):
```bash
git commit --no-verify
```

## Benefits:

- ✅ Ensures consistent code formatting across the team
- ✅ Prevents formatting-related CI failures
- ✅ Automatic - no need to remember to run `make black`
- ✅ Fast feedback - catches formatting issues before push