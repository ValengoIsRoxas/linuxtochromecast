#!/usr/bin/env bash
# Instala ltc y la opción "Enviar a Chromecast" del gestor de archivos.
# Todo va al home del usuario: no pide sudo ni toca el sistema.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$HOME/.local/bin"
VENV="$HERE/.venv"

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1"; }
die()  { printf '\n\033[31m✗\033[0m %s\n' "$1" >&2; exit 1; }

bold "Instalando linuxtochromecast"
echo

# --- dependencias del sistema ---

command -v python3 >/dev/null || die "Falta python3."

PYVER="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
    || die "Necesito Python 3.10 o más nuevo (tenés $PYVER)."
ok "python3 $PYVER"

if ! command -v ffmpeg >/dev/null || ! command -v ffprobe >/dev/null; then
    echo
    warn "Falta ffmpeg. Instalalo con alguno de estos:"
    echo "      Fedora:        sudo dnf install ffmpeg-free"
    echo "      Ubuntu/Debian: sudo apt install ffmpeg"
    echo "      Arch:          sudo pacman -S ffmpeg"
    die "Instalá ffmpeg y volvé a correr este script."
fi
ok "ffmpeg $(ffmpeg -version | head -1 | awk '{print $3}')"

# --- entorno virtual ---

if [[ ! -d "$VENV" ]]; then
    python3 -m venv "$VENV" || die "No pude crear el entorno virtual en $VENV"
fi
"$VENV/bin/pip" install --quiet --upgrade pip >/dev/null 2>&1 || true
"$VENV/bin/pip" install --quiet -r "$HERE/requirements.txt" \
    || die "Falló la instalación de dependencias de Python."
ok "dependencias de Python instaladas"

# --- comandos ---

mkdir -p "$BIN"

cat > "$BIN/ltc" <<EOF
#!/usr/bin/env bash
# Generado por install.sh de linuxtochromecast
exec "$VENV/bin/python" -m ltc "\$@"
EOF
chmod +x "$BIN/ltc"

install -m 755 "$HERE/integration/ltc-open" "$BIN/ltc-open"
ok "comandos ltc y ltc-open en $BIN"

# El módulo tiene que ser importable desde el venv. Un .pth apuntando al
# directorio del proyecto alcanza, y así los cambios en el código se ven al toque
# sin tener que reinstalar nada.
SITE="$("$VENV/bin/python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
[[ -d "$SITE" ]] || die "No encontré site-packages en el entorno virtual."
echo "$HERE" > "$SITE/linuxtochromecast.pth"

# --- integración con el gestor de archivos ---

APPS="$HOME/.local/share/applications"
mkdir -p "$APPS"
install -m 644 "$HERE/integration/linuxtochromecast.desktop" \
    "$APPS/linuxtochromecast.desktop"
command -v update-desktop-database >/dev/null && \
    update-desktop-database "$APPS" >/dev/null 2>&1 || true
ok "«Abrir con → Enviar a Chromecast» (todos los gestores)"

# Nautilus (GNOME): aparece en clic derecho → Scripts
NAUTILUS="$HOME/.local/share/nautilus/scripts"
if [[ -d "$HOME/.local/share/nautilus" ]] || command -v nautilus >/dev/null; then
    mkdir -p "$NAUTILUS"
    cat > "$NAUTILUS/Enviar a Chromecast" <<EOF
#!/usr/bin/env bash
# Generado por install.sh de linuxtochromecast
IFS=\$'\n'
for f in \$NAUTILUS_SCRIPT_SELECTED_FILE_PATHS; do
    [[ -n "\$f" ]] && exec "$BIN/ltc-open" "\$f"
done
EOF
    chmod +x "$NAUTILUS/Enviar a Chromecast"
    ok "Nautilus: clic derecho → Scripts → Enviar a Chromecast"
fi

# Dolphin (KDE)
if command -v dolphin >/dev/null || [[ -d "$HOME/.local/share/kio" ]]; then
    KIO="$HOME/.local/share/kio/servicemenus"
    mkdir -p "$KIO"
    install -m 755 "$HERE/integration/linuxtochromecast-servicemenu.desktop" \
        "$KIO/linuxtochromecast.desktop"
    ok "Dolphin: clic derecho → Enviar a Chromecast"
fi

# Thunar (XFCE) guarda sus acciones en un XML propio; lo dejamos documentado.
if command -v thunar >/dev/null; then
    warn "Thunar: agregá la acción a mano en Editar → Acciones personalizadas"
    echo "        Comando:  ltc-open %f     Patrón: *    Marcar: Archivos de vídeo"
fi

# --- PATH ---

echo
if ! printf '%s' ":$PATH:" | grep -q ":$BIN:"; then
    warn "$BIN no está en tu PATH. Agregalo con:"
    echo
    echo "    echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.bashrc && exec bash"
    echo
else
    ok "$BIN ya está en el PATH"
fi

bold "Listo."
echo
echo "  ltc pelicula.mkv              transmitir con subtítulos"
echo "  ltc pelicula.mkv --list-tracks  ver las pistas del archivo"
echo "  ltc --list-devices            ver los Chromecast de la red"
echo
