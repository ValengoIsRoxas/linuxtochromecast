#!/usr/bin/env bash
# Genera los videos de prueba que usan los tests.
#
# Cada uno está armado para forzar un camino distinto del código:
#
#   compatible.mp4   h264 4:2:0 + aac en MP4  → direct play, sin tocar nada
#   remuxeable.mkv   los mismos códecs en MKV → remux, sólo cambia el envoltorio
#   pelicula.mkv     h264 4:4:4 + ac3         → transcode, hay que recodificar
#
# Los .srt externos se escriben a propósito en latin-1, que es como vienen casi
# todos los subtítulos en castellano que circulan por ahí, y es justo el caso que
# rompe a los detectores automáticos de codificación.

set -euo pipefail

OUT="${1:-$(dirname "$0")/media}"
mkdir -p "$OUT"

command -v ffmpeg >/dev/null || { echo "Falta ffmpeg." >&2; exit 1; }

ff() { ffmpeg -y -hide_banner -loglevel error "$@"; }

# --- subtítulos ---------------------------------------------------------------

# En latin-1 (\xe9 = é, \xbf = ¿, \xf1 = ñ, \xed = í, \xf3 = ó).
printf '1\n00:00:01,000 --> 00:00:03,000\nHola beb\xe9, \xbfqu\xe9 tal?\n\n2\n00:00:04,000 --> 00:00:07,000\nAc\xe1 va otra l\xednea\n' \
    > "$OUT/compatible.es.srt"

printf '1\n00:00:02,000 --> 00:00:05,500\n<i>Ma\xf1ana</i> ser\xe1 otro d\xeda\n\n2\n00:00:06,000 --> 00:00:09,000\n{\\an8}El ni\xf1o comi\xf3 pi\xf1a\n\n3\n00:00:12,000 --> 00:00:15,000\nAdi\xf3s, coraz\xf3n\n' \
    > "$OUT/pelicula.es.srt"

# En UTF-8, para embeber dentro de los contenedores.
printf '1\n00:00:01,000 --> 00:00:04,000\nPista embebida\n\n2\n00:00:08,000 --> 00:00:11,000\nSegunda línea embebida\n' \
    > "$OUT/emb.srt"

# --- videos -------------------------------------------------------------------

# Ya compatible: se sirve tal cual, con seek nativo por rangos HTTP.
ff -f lavfi -i "testsrc=size=320x240:rate=25:duration=10" \
   -f lavfi -i "sine=frequency=440:duration=10" \
   -c:v libx264 -preset ultrafast -pix_fmt yuv420p -c:a aac -shortest \
   "$OUT/compatible.mp4"

# Códecs compatibles pero dentro de un MKV: sólo hay que reempaquetar.
ff -f lavfi -i "testsrc=size=320x240:rate=25:duration=8" \
   -f lavfi -i "sine=frequency=440:duration=8" -i "$OUT/emb.srt" \
   -map 0:v -map 1:a -map 2:s \
   -c:v libx264 -preset ultrafast -pix_fmt yuv420p -c:a aac -c:s srt \
   -metadata:s:s:0 language=spa -shortest \
   "$OUT/remuxeable.mkv"

# Nada compatible: 4:4:4 (el receptor sólo decodifica 4:2:0) y audio AC3.
# Ojo que sin -pix_fmt explícito, libx264 elige yuv444p: por eso este archivo
# sale incompatible "solo".
ff -f lavfi -i "testsrc=size=640x360:rate=25:duration=20" \
   -f lavfi -i "sine=frequency=440:duration=20" -i "$OUT/emb.srt" \
   -map 0:v -map 1:a -map 2:s \
   -c:v libx264 -preset ultrafast -c:a ac3 -c:s srt \
   -metadata:s:s:0 language=spa -metadata:s:s:0 title="Español" -shortest \
   "$OUT/pelicula.mkv"

echo "Archivos de prueba en $OUT:"
for f in "$OUT"/*; do
    printf '  %-20s %s\n' "$(basename "$f")" "$(du -h "$f" | cut -f1)"
done
