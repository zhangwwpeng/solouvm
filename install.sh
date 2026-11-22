#!/usr/bin/env bash
# Offline/directory installer for solouvm (--user, --system, --prefix, --uninstall).
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: ./install.sh [--user | --system | --prefix DIR] [--python PATH] [--uninstall]

Options:
  --user          Install for the current user under "$HOME/.local" (default)
  --system        Install system-wide under "/usr/local" (may require sudo)
  --prefix DIR    Install under custom directory DIR
  --python PATH   Use specific Python >= 3.10 interpreter
  --uninstall     Remove solouvm from the target prefix
  -h, --help      Show this help message and exit
EOF
}

die() {
    echo "solouvm install: error: $*" >&2
    exit 2
}

mode=""
explicit_mode=""
prefix=""
explicit_prefix=""
python_arg=""
uninstall=0

while [ $# -gt 0 ]; do
    case "$1" in
        --user)
            if [ -n "$explicit_mode" ] && [ "$explicit_mode" != "user" ]; then
                die "cannot combine --user with --system"
            fi
            explicit_mode="user"
            shift
            ;;
        --system)
            if [ -n "$explicit_mode" ] && [ "$explicit_mode" != "system" ]; then
                die "cannot combine --system with --user"
            fi
            explicit_mode="system"
            shift
            ;;
        --prefix)
            if [ $# -lt 2 ] || [ -z "$2" ]; then
                die "option --prefix requires a non-empty directory"
            fi
            explicit_prefix="$2"
            shift 2
            ;;
        --prefix=*)
            explicit_prefix="${1#--prefix=}"
            if [ -z "$explicit_prefix" ]; then
                die "option --prefix requires a non-empty directory"
            fi
            shift
            ;;
        --python)
            if [ $# -lt 2 ] || [ -z "$2" ]; then
                die "option --python requires a non-empty path"
            fi
            python_arg="$2"
            shift 2
            ;;
        --python=*)
            python_arg="${1#--python=}"
            if [ -z "$python_arg" ]; then
                die "option --python requires a non-empty path"
            fi
            shift
            ;;
        --uninstall)
            uninstall=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            die "unknown option: $1"
            ;;
    esac
done

if [ -n "$explicit_prefix" ] && [ -n "$explicit_mode" ]; then
    die "cannot combine --prefix with --$explicit_mode"
fi

if [ -n "$explicit_prefix" ]; then
    mode="prefix"
    prefix="$explicit_prefix"
elif [ "$explicit_mode" = "system" ]; then
    mode="system"
    prefix="/usr/local"
else
    mode="user"
    prefix="$HOME/.local"
fi

case "$prefix" in
    *$'\n'*|*$'\r'*)
        die "invalid prefix path: newlines are not allowed"
        ;;
    /*)
        ;;
    *)
        prefix="$(pwd -P)/$prefix"
        ;;
esac
while [ "$prefix" != "/" ] && [ "${prefix%/}" != "$prefix" ]; do
    prefix="${prefix%/}"
done

if [ -n "$python_arg" ]; then
    case "$python_arg" in
        *$'\n'*|*$'\r'*)
            die "invalid --python path: newlines are not allowed"
            ;;
    esac
fi

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
if [ "$uninstall" -eq 0 ] && [ ! -d "$SRC_DIR/solouvm" ]; then
    die "missing 'solouvm/' package directory alongside install.sh ($SRC_DIR)"
fi

LIB_PARENT="$prefix/lib"
INSTALL_LIB="$LIB_PARENT/solouvm"
INSTALL_BIN="$prefix/bin"
BIN_PATH="$INSTALL_BIN/solouvm"
ZSH_COMP_DIR="$prefix/share/zsh/site-functions"
ZSH_COMP_PATH="$ZSH_COMP_DIR/_solouvm"
BASH_COMP_DIR="$prefix/share/bash-completion/completions"
BASH_COMP_PATH="$BASH_COMP_DIR/solouvm"

nearest_existing_dir() {
    local p="$1"
    while [ ! -e "$p" ]; do
        local parent
        parent="$(dirname "$p")"
        if [ "$parent" = "$p" ]; then
            break
        fi
        p="$parent"
    done
    printf '%s' "$p"
}

check_dir_writable() {
    local target="$1"
    local label="$2"
    local anchor
    anchor="$(nearest_existing_dir "$target")"
    if [ ! -d "$anchor" ] || [ ! -w "$anchor" ]; then
        if [ "$mode" = "system" ]; then
            die "no write permission for $label ($anchor). Run 'sudo ./install.sh --system' or use '--user'."
        fi
        die "no write permission for $label ($anchor)."
    fi
}

if [ "$uninstall" -eq 1 ]; then
    if [ -e "$INSTALL_LIB" ] || [ -L "$INSTALL_LIB" ]; then
        check_dir_writable "$LIB_PARENT" "library directory"
    fi
    if [ -e "$BIN_PATH" ] || [ -L "$BIN_PATH" ]; then
        check_dir_writable "$INSTALL_BIN" "binary directory"
    fi
    if [ -e "$ZSH_COMP_PATH" ] || [ -L "$ZSH_COMP_PATH" ]; then
        check_dir_writable "$ZSH_COMP_DIR" "zsh completion directory"
    fi
    if [ -e "$BASH_COMP_PATH" ] || [ -L "$BASH_COMP_PATH" ]; then
        check_dir_writable "$BASH_COMP_DIR" "bash completion directory"
    fi
    removed=0
    if [ -e "$INSTALL_LIB" ] || [ -L "$INSTALL_LIB" ]; then
        rm -rf "$INSTALL_LIB"
        echo "Removed $INSTALL_LIB"
        removed=1
    fi
    if [ -e "$BIN_PATH" ] || [ -L "$BIN_PATH" ]; then
        rm -f "$BIN_PATH"
        echo "Removed $BIN_PATH"
        removed=1
    fi
    if [ -e "$ZSH_COMP_PATH" ] || [ -L "$ZSH_COMP_PATH" ]; then
        rm -f "$ZSH_COMP_PATH"
        echo "Removed $ZSH_COMP_PATH"
        removed=1
    fi
    if [ -e "$BASH_COMP_PATH" ] || [ -L "$BASH_COMP_PATH" ]; then
        rm -f "$BASH_COMP_PATH"
        echo "Removed $BASH_COMP_PATH"
        removed=1
    fi
    if [ "$mode" = "user" ]; then
        for py_cand in python3 python3.13 python3.12 python3.11 python3.10; do
            if command -v "$py_cand" >/dev/null 2>&1; then
                user_site="$("$py_cand" -c 'import site; print(site.getusersitepackages())' 2>/dev/null || true)"
                if [ -n "$user_site" ] && [ -f "$user_site/solouvm.pth" ]; then
                    if grep -Fx "$INSTALL_LIB" "$user_site/solouvm.pth" >/dev/null 2>&1; then
                        rm -f "$user_site/solouvm.pth"
                        echo "Removed $user_site/solouvm.pth"
                        removed=1
                    fi
                fi
            fi
        done
    fi
    if [ "$removed" -eq 0 ]; then
        echo "solouvm is not installed under $prefix"
    else
        echo "Uninstalled solouvm ($mode) from $prefix"
    fi
    exit 0
fi

check_python_bin() {
    local cand="$1"
    if [ -z "$cand" ]; then
        return 1
    fi
    if ! "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
        return 1
    fi
    "$cand" -c 'import os, sys; print(os.path.abspath(sys.executable))' 2>/dev/null
}

is_non_venv_python() {
    "$1" -c 'import sys; raise SystemExit(0 if sys.prefix == getattr(sys, "base_prefix", sys.prefix) else 1)' >/dev/null 2>&1
}

is_under_home() {
    case "$1" in
        "$HOME"/*) return 0 ;;
        *) return 1 ;;
    esac
}

is_other_executable() {
    "$1" -c 'import os, stat, sys; st = os.stat(sys.executable); raise SystemExit(0 if (st.st_mode & stat.S_IXOTH) else 1)' >/dev/null 2>&1
}

resolve_python() {
    if [ -n "$python_arg" ]; then
        local resolved_arg=""
        if [[ "$python_arg" == */* ]]; then
            [ -x "$python_arg" ] || die "python binary is not executable: $python_arg"
            resolved_arg="$(check_python_bin "$python_arg")" || die "python binary must be Python >= 3.10: $python_arg"
        else
            local found_cmd=""
            found_cmd="$(command -v "$python_arg" 2>/dev/null || true)"
            [ -n "$found_cmd" ] || die "python binary not found: $python_arg"
            resolved_arg="$(check_python_bin "$found_cmd")" || die "python binary must be Python >= 3.10: $python_arg"
        fi
        if [ "$mode" = "system" ]; then
            if is_under_home "$resolved_arg" || ! is_other_executable "$resolved_arg"; then
                echo "solouvm install: warning: --python $resolved_arg may not be accessible to all users in --system mode" >&2
            fi
        fi
        printf '%s' "$resolved_arg"
        return 0
    fi

    local candidates=()
    local name resolved_path resolved_real
    if [ "$mode" = "system" ]; then
        candidates=(
            "/usr/bin/python3"
            "/usr/local/bin/python3"
            "/opt/homebrew/bin/python3"
            "python3"
            "python3.13"
            "python3.12"
            "python3.11"
            "python3.10"
        )
        for name in "${candidates[@]}"; do
            if [[ "$name" == /* ]]; then
                [ -x "$name" ] || continue
                resolved_path="$name"
            else
                resolved_path="$(command -v "$name" 2>/dev/null || true)"
                [ -n "$resolved_path" ] || continue
            fi
            resolved_real="$(check_python_bin "$resolved_path" || true)"
            [ -n "$resolved_real" ] || continue
            if is_under_home "$resolved_path" || is_under_home "$resolved_real"; then
                continue
            fi
            if ! is_non_venv_python "$resolved_real"; then
                continue
            fi
            printf '%s' "$resolved_real"
            return 0
        done
        die "no global Python >= 3.10 found outside \$HOME for --system install; install system Python >= 3.10 or pass --python /shared/path/to/python3"
    fi

    candidates=(
        "python3"
        "python3.13"
        "python3.12"
        "python3.11"
        "python3.10"
        "/opt/homebrew/bin/python3"
        "/usr/local/bin/python3"
        "/usr/bin/python3"
    )
    local fallback=""
    for name in "${candidates[@]}"; do
        if [[ "$name" == /* ]]; then
            [ -x "$name" ] || continue
            resolved_path="$name"
        else
            resolved_path="$(command -v "$name" 2>/dev/null || true)"
            [ -n "$resolved_path" ] || continue
        fi
        resolved_real="$(check_python_bin "$resolved_path" || true)"
        [ -n "$resolved_real" ] || continue
        if is_non_venv_python "$resolved_real"; then
            printf '%s' "$resolved_real"
            return 0
        fi
        if [ -z "$fallback" ]; then
            fallback="$resolved_real"
        fi
    done
    if [ -n "$fallback" ]; then
        printf '%s' "$fallback"
        return 0
    fi
    die "Python >= 3.10 is required; none found in PATH or standard system locations"
}

check_dir_writable "$INSTALL_LIB" "library directory"
check_dir_writable "$INSTALL_BIN" "binary directory"
check_dir_writable "$ZSH_COMP_DIR" "zsh completion directory"
check_dir_writable "$BASH_COMP_DIR" "bash completion directory"
if [ -e "$INSTALL_LIB" ] && [ ! -w "$LIB_PARENT" ]; then
    if [ "$mode" = "system" ]; then
        die "no write permission on $LIB_PARENT to upgrade existing install. Run 'sudo ./install.sh --system'."
    fi
    die "no write permission on $LIB_PARENT to upgrade existing install."
fi
if [ -e "$BIN_PATH" ] && [ ! -w "$INSTALL_BIN" ]; then
    if [ "$mode" = "system" ]; then
        die "no write permission on $INSTALL_BIN to update $BIN_PATH. Run 'sudo ./install.sh --system'."
    fi
    die "no write permission on $INSTALL_BIN to update $BIN_PATH."
fi

PYTHON_BIN="$(resolve_python)"

VERSION="$("$PYTHON_BIN" -c '
import re, sys
from pathlib import Path
text = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")
m = re.search(r"^__version__\s*=\s*[\x22\x27]([^\x22\x27]+)[\x22\x27]", text, re.MULTILINE)
print(m.group(1) if m else "0.1.0")
' "$SRC_DIR/solouvm/__init__.py")"

PREV_VERSION=""
if [ -f "$INSTALL_LIB/.install-meta" ]; then
    PREV_VERSION="$(sed -n 's/^version=//p' "$INSTALL_LIB/.install-meta" | head -n 1 || true)"
fi

if [ -n "$PREV_VERSION" ]; then
    echo "Upgrading solouvm $PREV_VERSION -> $VERSION ($mode mode, prefix: $prefix)"
else
    echo "Installing solouvm $VERSION ($mode mode, prefix: $prefix)"
fi

STAGE_DIR=""
BACKUP_DIR=""
BIN_TMP=""
ZSH_COMP_TMP=""
BASH_COMP_TMP=""
cleanup_stage() {
    if [ -n "$STAGE_DIR" ] && [ -d "$STAGE_DIR" ]; then
        rm -rf "$STAGE_DIR" 2>/dev/null || true
    fi
    if [ -n "$BIN_TMP" ] && [ -e "$BIN_TMP" ]; then
        rm -f "$BIN_TMP" 2>/dev/null || true
    fi
    if [ -n "$ZSH_COMP_TMP" ] && [ -e "$ZSH_COMP_TMP" ]; then
        rm -f "$ZSH_COMP_TMP" 2>/dev/null || true
    fi
    if [ -n "$BASH_COMP_TMP" ] && [ -e "$BASH_COMP_TMP" ]; then
        rm -f "$BASH_COMP_TMP" 2>/dev/null || true
    fi
}
trap cleanup_stage EXIT INT TERM

mkdir -p "$LIB_PARENT" "$INSTALL_BIN"
STAGE_DIR="$LIB_PARENT/.solouvm.tmp.$$"
rm -rf "$STAGE_DIR"
mkdir -p "$STAGE_DIR"

cp -R "$SRC_DIR/solouvm" "$STAGE_DIR/solouvm"
find "$STAGE_DIR/solouvm" -name '__pycache__' -type d -exec rm -rf {} +
find "$STAGE_DIR/solouvm" -name '*.pyc' -type f -delete

if [ -d "$SRC_DIR/doc" ]; then
    cp -R "$SRC_DIR/doc" "$STAGE_DIR/doc"
fi
if [ -d "$SRC_DIR/skills" ]; then
    cp -R "$SRC_DIR/skills" "$STAGE_DIR/skills"
fi
if [ -f "$SRC_DIR/README.md" ]; then
    cp "$SRC_DIR/README.md" "$STAGE_DIR/README.md"
fi
if [ -f "$SRC_DIR/LICENSE" ]; then
    cp "$SRC_DIR/LICENSE" "$STAGE_DIR/LICENSE"
fi

echo "Precompiling Python bytecode (.pyc)..."
"$PYTHON_BIN" -m compileall -q -f -d "$INSTALL_LIB/solouvm" "$STAGE_DIR/solouvm"

INSTALLED_AT="$("$PYTHON_BIN" -c 'from datetime import datetime; print(datetime.now().astimezone().isoformat(timespec="seconds"))')"
cat >"$STAGE_DIR/.install-meta" <<EOF
schema=1
version=$VERSION
mode=$mode
prefix=$prefix
lib_dir=$INSTALL_LIB
bin_path=$BIN_PATH
zsh_completion=$ZSH_COMP_PATH
bash_completion=$BASH_COMP_PATH
python=$PYTHON_BIN
installed_at=$INSTALLED_AT
EOF

chmod -R a+rX "$STAGE_DIR"
if [ -f "$STAGE_DIR/solouvm/backends/fake_sim.sh" ]; then
    chmod 755 "$STAGE_DIR/solouvm/backends/fake_sim.sh"
fi

"$PYTHON_BIN" -c 'import sys; lib = sys.argv.pop(1); sys.argv[0] = "solouvm"; sys.path.insert(0, lib); from solouvm.cli import main; raise SystemExit(main(["--version"]))' "$STAGE_DIR" >/dev/null

mkdir -p "$ZSH_COMP_DIR" "$BASH_COMP_DIR"
ZSH_COMP_TMP="$ZSH_COMP_DIR/.solouvm.tmp.$$"
BASH_COMP_TMP="$BASH_COMP_DIR/.solouvm.tmp.$$"
"$PYTHON_BIN" -c 'import sys; lib = sys.argv.pop(1); shell = sys.argv.pop(1); sys.argv[0] = "solouvm"; sys.path.insert(0, lib); from solouvm.cli import main; raise SystemExit(main(["completion", shell]))' "$STAGE_DIR" zsh >"$ZSH_COMP_TMP"
"$PYTHON_BIN" -c 'import sys; lib = sys.argv.pop(1); shell = sys.argv.pop(1); sys.argv[0] = "solouvm"; sys.path.insert(0, lib); from solouvm.cli import main; raise SystemExit(main(["completion", shell]))' "$STAGE_DIR" bash >"$BASH_COMP_TMP"
chmod 644 "$ZSH_COMP_TMP" "$BASH_COMP_TMP"

if [ -e "$INSTALL_LIB" ] || [ -L "$INSTALL_LIB" ]; then
    BACKUP_DIR="$LIB_PARENT/.solouvm.bak.$$"
    rm -rf "$BACKUP_DIR"
    mv "$INSTALL_LIB" "$BACKUP_DIR"
    if ! mv "$STAGE_DIR" "$INSTALL_LIB"; then
        mv "$BACKUP_DIR" "$INSTALL_LIB" 2>/dev/null || true
        die "failed to activate staged installation at $INSTALL_LIB"
    fi
    rm -rf "$BACKUP_DIR"
    BACKUP_DIR=""
else
    mv "$STAGE_DIR" "$INSTALL_LIB"
fi
STAGE_DIR=""

ESCAPED_PYTHON="$("$PYTHON_BIN" -c 'import shlex, sys; print(shlex.quote(sys.argv[1]))' "$PYTHON_BIN")"
ESCAPED_LIB="$("$PYTHON_BIN" -c 'import shlex, sys; print(shlex.quote(sys.argv[1]))' "$INSTALL_LIB")"
BIN_TMP="$INSTALL_BIN/.solouvm.tmp.$$"
cat >"$BIN_TMP" <<EOF
#!/usr/bin/env sh
set -eu
exec $ESCAPED_PYTHON -c 'import sys; lib = sys.argv.pop(1); sys.argv[0] = "solouvm"; sys.path.insert(0, lib); from solouvm.cli import main; raise SystemExit(main())' $ESCAPED_LIB "\$@"
EOF
chmod 755 "$BIN_TMP"
mv "$BIN_TMP" "$BIN_PATH"
BIN_TMP=""

mv "$ZSH_COMP_TMP" "$ZSH_COMP_PATH"
ZSH_COMP_TMP=""
mv "$BASH_COMP_TMP" "$BASH_COMP_PATH"
BASH_COMP_TMP=""

if [ "$mode" = "user" ]; then
    USER_SITE="$("$PYTHON_BIN" -c 'import site; print(site.getusersitepackages())' 2>/dev/null || true)"
    if [ -n "$USER_SITE" ] && is_under_home "$USER_SITE"; then
        if mkdir -p "$USER_SITE" 2>/dev/null && [ -w "$USER_SITE" ]; then
            printf '%s\n' "$INSTALL_LIB" >"$USER_SITE/solouvm.pth"
        fi
    fi
fi

if [ "$mode" = "system" ]; then
    chmod a+rX "$LIB_PARENT" "$INSTALL_BIN" "$prefix/share" "$prefix/share/zsh" "$ZSH_COMP_DIR" "$prefix/share/bash-completion" "$BASH_COMP_DIR" 2>/dev/null || true
fi

echo ""
echo "solouvm $VERSION installed successfully!"
echo "  Mode:        $mode"
echo "  Executable:  $BIN_PATH"
echo "  Library:     $INSTALL_LIB (with precompiled .pyc)"
echo "  Python:      $PYTHON_BIN"
echo "  Zsh comp:    $ZSH_COMP_PATH"
echo "  Bash comp:   $BASH_COMP_PATH"
echo ""

path_has_dir=0
case ":${PATH:-}:" in
    *":$INSTALL_BIN:"*) path_has_dir=1 ;;
esac

if [ "$mode" = "user" ]; then
    if [ "$path_has_dir" -eq 1 ]; then
        echo "PATH check: OK ($INSTALL_BIN is already in your current PATH)."
    else
        echo "PATH check: $INSTALL_BIN is NOT in your current PATH."
    fi
    shell_name="$(basename "${SHELL:-sh}")"
    case "$shell_name" in
        zsh)  primary_rc="$HOME/.zshrc"; alt_rc="$HOME/.bashrc" ;;
        bash) primary_rc="$HOME/.bashrc"; alt_rc="$HOME/.zshrc" ;;
        *)    primary_rc="$HOME/.zshrc or $HOME/.bashrc"; alt_rc="" ;;
    esac
    for rc_file in "$HOME/.zshrc" "$HOME/.bashrc"; do
        rc_label="$(basename "$rc_file")"
        if [ -f "$rc_file" ] && grep -F '.local/bin' "$rc_file" >/dev/null 2>&1; then
            echo "  - ~/$rc_label: contains '.local/bin'"
        elif [ -f "$rc_file" ]; then
            echo "  - ~/$rc_label: missing '$INSTALL_BIN'"
        else
            echo "  - ~/$rc_label: not found"
        fi
    done
    if [ "$path_has_dir" -eq 0 ]; then
        echo ""
        echo "To enable 'solouvm' in $primary_rc${alt_rc:+ (or $alt_rc)}, add:"
        echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
        echo "Then reload your shell:"
        echo "  source ~/.zshrc   # or: source ~/.bashrc"
    fi

    echo ""
    echo "Shell completion:"
    if [ -f "$HOME/.zshrc" ] && (grep -F '.local/share/zsh/site-functions' "$HOME/.zshrc" >/dev/null 2>&1 || grep -F 'solouvm completion zsh' "$HOME/.zshrc" >/dev/null 2>&1); then
        echo "  - ~/.zshrc: zsh completion looks configured"
    else
        echo "  - ~/.zshrc: add to enable zsh completion:"
        echo "      fpath=(\"\$HOME/.local/share/zsh/site-functions\" \$fpath)"
        echo "      autoload -Uz compinit && compinit"
    fi
    if [ -f "$HOME/.bashrc" ] && (grep -F '.local/share/bash-completion/completions/solouvm' "$HOME/.bashrc" >/dev/null 2>&1 || grep -F 'solouvm completion bash' "$HOME/.bashrc" >/dev/null 2>&1); then
        echo "  - ~/.bashrc: bash completion looks configured"
    else
        echo "  - ~/.bashrc: auto-loaded if bash-completion >= 2 is installed; otherwise add:"
        echo "      [ -f \"\$HOME/.local/share/bash-completion/completions/solouvm\" ] && source \"\$HOME/.local/share/bash-completion/completions/solouvm\""
    fi
elif [ "$mode" = "system" ]; then
    if [ "$path_has_dir" -eq 1 ]; then
        echo "PATH check: OK ($INSTALL_BIN is in your current PATH)."
    else
        echo "PATH check: $INSTALL_BIN is NOT in your current PATH."
        echo "Ensure '$INSTALL_BIN' is added to PATH (e.g. in /etc/profile or ~/.zshrc / ~/.bashrc):"
        echo "  export PATH=\"$INSTALL_BIN:\$PATH\""
    fi
    echo ""
    echo "Shell completion:"
    echo "  Installed to standard system directories ($ZSH_COMP_PATH and $BASH_COMP_PATH)."
    echo "  New shells with compinit / bash-completion enabled will pick them up automatically."
    echo "  Manual fallback: source <(solouvm completion zsh)   # or: eval \"\$(solouvm completion bash)\""
else
    if [ "$path_has_dir" -eq 1 ]; then
        echo "PATH check: OK ($INSTALL_BIN is in your current PATH)."
    else
        echo "PATH check: $INSTALL_BIN is NOT in your current PATH."
        echo "Ensure '$INSTALL_BIN' is added to PATH (e.g. in /etc/profile or ~/.zshrc / ~/.bashrc):"
        echo "  export PATH=\"$INSTALL_BIN:\$PATH\""
    fi
    echo ""
    echo "Shell completion:"
    echo "  Zsh (~/.zshrc):"
    echo "    fpath=(\"$ZSH_COMP_DIR\" \$fpath)"
    echo "    autoload -Uz compinit && compinit"
    echo "  Bash (~/.bashrc):"
    echo "    source \"$BASH_COMP_PATH\""
fi

resolved_cmd="$(command -v solouvm 2>/dev/null || true)"
if [ -n "$resolved_cmd" ] && [ "$resolved_cmd" != "$BIN_PATH" ]; then
    echo ""
    echo "WARNING: 'solouvm' currently resolves to '$resolved_cmd' ahead of '$BIN_PATH'."
    echo "         Check your PATH order or uninstall the other copy to avoid running the wrong version."
fi
