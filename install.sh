#!/usr/bin/env bash
# Instala ltc con pipx y lanza el asistente de configuración (`ltc setup`).
#
#   ./install.sh            instala y pregunta lo que se puede configurar
#   ./install.sh --yes      sin preguntas: todo por defecto
#   ./install.sh --dev      instala editable: los cambios al código se ven al toque
#   ./install.sh --no-setup instala y no corre el asistente (después: ltc setup)
#
# Todo va al home del usuario. Sólo pide sudo, y sólo si se acepta, para
# instalar paquetes del sistema que falten (ffmpeg, pipx).

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACKAGE="linuxtochromecast"

YES=0; DEV=0; SETUP=1
for arg in "$@"; do
    case "$arg" in
        -y|--yes)    YES=1 ;;
        --dev)       DEV=1 ;;
        --no-setup)  SETUP=0 ;;
        -h|--help)   sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Unknown option: $arg (see --help)" >&2; exit 2 ;;
    esac
done

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1"; }
die()  { printf '\n\033[31m✗\033[0m %s\n' "$1" >&2; exit 1; }

# Pregunta sí/no. Sin terminal, o con --yes, responde que sí.
confirm() {
    [[ $YES -eq 1 || ! -t 0 ]] && return 0
    local answer
    read -r -p "  $1 [Y/n] " answer || return 0
    [[ -z "$answer" || "$answer" =~ ^[YySs] ]]
}

bold "Installing linuxtochromecast"
echo

# --- gestor de paquetes (sólo para sugerir o instalar lo que falte) ---

PM=""
for candidate in dnf apt-get pacman zypper; do
    command -v "$candidate" >/dev/null && { PM="$candidate"; break; }
done

# system_install FFMPEG_PKG PIPX_PKG... según la distro; devuelve 1 si no sabe.
system_install() {
    case "$PM" in
        dnf)     sudo dnf install -y "$@" ;;
        apt-get) sudo apt-get install -y "$@" ;;
        pacman)  sudo pacman -S --needed --noconfirm "$@" ;;
        zypper)  sudo zypper install -y "$@" ;;
        *)       return 1 ;;
    esac
}

pkg_name() {   # pkg_name ffmpeg|pipx
    case "$PM:$1" in
        dnf:ffmpeg)     echo ffmpeg-free ;;
        dnf:pipx|apt-get:pipx)   echo pipx ;;
        pacman:pipx)    echo python-pipx ;;
        zypper:pipx)    echo python3-pipx ;;
        *)              echo "$1" ;;
    esac
}

need() {   # need ffmpeg|pipx
    local what="$1" pkg
    pkg="$(pkg_name "$what")"
    echo
    if [[ -n "$PM" ]] && confirm "$what is missing. Install it now ($PM install $pkg, asks for your password)?"; then
        system_install "$pkg" || die "Couldn't install $pkg. Install $what yourself and run this again."
    else
        die "$what is required. Install it and run this script again."
    fi
}

# --- Python ---

command -v python3 >/dev/null || die "python3 is missing."
PYVER="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
    || die "ltc needs Python 3.11 or newer (you have $PYVER). Install a newer python3 from your distro."
ok "python3 $PYVER"

# --- ffmpeg ---

if ! command -v ffmpeg >/dev/null || ! command -v ffprobe >/dev/null; then
    need ffmpeg
fi
ok "ffmpeg $(ffmpeg -version | head -1 | awk '{print $3}')"

# --- pipx ---

command -v pipx >/dev/null || need pipx
ok "pipx $(pipx --version)"

# --- instalación anterior (script con venv dentro del repo) ---
# Ese `ltc` era un wrapper generado por la versión vieja de este script; pipx
# no pisa un archivo ajeno, así que se quita.

OLD="$HOME/.local/bin/ltc"
if [[ -f "$OLD" && ! -L "$OLD" ]] && grep -q "Generado por install.sh" "$OLD" 2>/dev/null; then
    rm -f "$OLD"
    ok "removed the wrapper from the previous installation (the repo's .venv is no longer used)"
fi

# --- ltc ---

SRC=("$HERE")
[[ $DEV -eq 1 ]] && SRC=(--editable "$HERE")
pipx install --force --python "$(command -v python3)" "${SRC[@]}" >/dev/null \
    || die "pipx couldn't install ltc. Run: pipx install --force $HERE   to see why."
ok "ltc installed with pipx$([[ $DEV -eq 1 ]] && echo ' (editable)')"

BIN="$(pipx environment --value PIPX_BIN_DIR 2>/dev/null || true)"
BIN="${BIN:-$HOME/.local/bin}"
[[ -x "$BIN/ltc" ]] || die "ltc isn't in $BIN after installing; run: pipx list"

pipx ensurepath >/dev/null 2>&1 || true
ON_PATH=1
case ":$PATH:" in *":$BIN:"*) ;; *) ON_PATH=0 ;; esac

# --- asistente ---

if [[ $SETUP -eq 1 ]]; then
    ARGS=()
    [[ $YES -eq 1 ]] && ARGS+=(--yes)
    "$BIN/ltc" setup ${ARGS[@]+"${ARGS[@]}"}
else
    echo
    echo "  Run  ltc setup  to choose folders, device and the rest."
fi

if [[ $ON_PATH -eq 0 ]]; then
    echo
    warn "$BIN wasn't on your PATH. pipx added it for new terminals:"
    echo "      open a new terminal, or run:  export PATH=\"$BIN:\$PATH\""
fi

echo
bold "Done."
echo "  ltc                   open your library"
echo "  ltc movie.mkv         cast one file"
echo "  ltc config            change settings"
echo "  ltc uninstall         remove everything"
echo
