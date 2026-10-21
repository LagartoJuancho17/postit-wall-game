# Juego de pared con post-its

La webcam detecta los post-its en vivo y el proyector muestra esos mismos
contornos, sus colores y la pelota. Hay un post-it de inicio, uno de llegada
y rebotes opcionales. Las esquinas también se muestran si están presentes,
pero no son necesarias para jugar. Por defecto, el inicio es amarillo, la
llegada naranja, los rebotes verdes y las esquinas fucsia o violetas. Desde
la vista de cámara podés elegir otros colores. `J` abre la ventana `/projector`.
No hay pasos manuales de calibración: para que cada contorno caiga justo
alrededor de su papel, el panel alinea cámara y proyector solo, con unos
patrones de puntos que proyecta durante unos 8 segundos (`A` lo repite).

También hay una versión que corre entera en el navegador, sin instalar nada:
ver **Versión web (Vercel)**.

## Iniciar la interfaz web

Con Python 3.12 y `uv` instalados, ejecutá desde esta carpeta:

```sh
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
./.venv/bin/python web.py
```

Se abrirá Chrome en `http://127.0.0.1:8765/`. Elegí la Logitech C920 en el
selector de cámaras y permití el acceso a la cámara para esa página si Chrome
lo solicita. El navegador muestra la imagen en la pantalla principal. El
servidor Python nunca abre la cámara, así que no necesita permiso de cámara
de macOS. Recibe fotos JPEG del navegador en dos casos: al escanear y, unas
cuatro veces por segundo, para la detección en vivo.

Pulsá `J` o el botón **Abrir proyector**. Mové la nueva ventana a la pantalla
del proyector y activá su pantalla completa desde el botón de esa ventana.
Podés cambiar su tamaño o activar pantalla completa durante la partida: el
encuadre se adapta conservando las proporciones. Los controles permanecen en la
ventana principal. Si Chrome bloquea la ventana emergente, permití las
ventanas emergentes para `127.0.0.1` y volvé a pulsar `J`. Con la ventana
del proyector en pantalla completa, el panel alinea cámara y proyector (ver
**Alinear cámara y proyector**).

Debajo de "Preparar la pared", el panel muestra el siguiente paso. Cuando la
cámara ve un inicio y una llegada válidos, la pelota aparece automáticamente.

Con la cámara y la proyección listas:

1. Revisá en la vista en vivo que estén detectados el inicio y la llegada.
   Los contornos del proyector siguen esas mismas detecciones, incluso antes
   de que el tablero esté completo.
2. Ajustá la dirección desde el control principal y lanzá con `Espacio`.
3. Tras ganar o perder, `R` inicia otro intento con el tablero detectado más
   reciente. `S` permite releerlo expresamente sin apagar la proyección.

Si se cierra la ventana del proyector durante un intento, la pelota queda en
pausa. Reabrila con `J` y pulsá **Continuar intento** (o `Espacio`). Con el
intento en pausa también podés releer los post-its o empezar otro
intento con `R`.

La detección y sus contornos siguen en directo durante la partida. La física
usa el tablero con el que se lanzó la pelota: mover un papel cambia el
contorno en vivo, pero su rebote se actualiza al iniciar el siguiente intento.
Sin imágenes nuevas durante 1,5 segundos, desaparecen los contornos viejos.
El proyector muestra solo el fondo elegido, un borde neutro alrededor de cada
papel detectado y la pelota; el video de la cámara queda en el panel. Sin
alinear, encaja la imagen de la cámara en la ventana (con bandas si hace
falta) y avisa `Sin alinear · pulsá A en el panel`: los bordes no caen sobre
los papeles. Alineado, cada borde rodea su papel a 6 píxeles del proyector y
la pelota se mueve en coordenadas del proyector.

## Alinear cámara y proyector

La cámara y el proyector miran la pared desde lugares distintos. Para que un
borde caiga justo alrededor de su papel, el panel mide cómo se corresponden
sus píxeles (una homografía; el detalle está en
`docs/alignment-and-color.md`).

- **Cuándo.** Sola, la primera vez que la ventana del proyector está en
  pantalla completa con la cámara funcionando, y de nuevo si cambia el tamaño
  de la proyección, la cámara o su resolución. `A` o **Alinear proyector** la
  repite cuando quieras. Si movés la cámara o el proyector, pulsá `A`: ese
  movimiento el panel no lo nota solo.
- **Qué se ve.** El proyector muestra 14 patrones de puntos blancos sobre
  negro durante unos 8 segundos (más con una cámara en automático, como el
  iPhone). No muevas los equipos mientras tanto. Si la toma sale imprecisa o
  incoherente, el panel la repite una vez más despacio.
- **Resultado.** El panel dice el error medido, por ejemplo "Proyector
  alineado: error 0.2 px con 60 puntos". Si falla, dice por qué: la cámara
  vio pocos puntos (que vea toda la proyección y el proyector esté
  encendido), puntos incoherentes (reflejos), una toma imprecisa (fijá los
  equipos) o muy poca parte de la proyección (encuadrá la pared entera).
- **Color sobre el papel.** Después de alinear, el panel proyecta un cuadrado
  de cada color sobre pared vacía. Si la cámara toma alguno por un post-it,
  el proyector no pone color: sobre los papeles proyecta solo luz neutra
  (blanca con el fondo oscuro). Si ninguno se confunde, cada papel se ilumina
  con su color, siempre por dentro del papel. El panel dice cuál quedó.

El proyector nunca pone colores sobre la pared vacía, así su propia luz no
crea post-its falsos. Medido en el simulador de lazo cerrado
(`services/simulation`), en sala normal, oscura e iluminada: el borde queda a
1.6-3.8 px de donde debe y nunca toca el papel. Con el diseño anterior, en
la pared real la detección cambió de estado 730 veces en 26 minutos
(`runs/web-events.jsonl`, 19/10), y en el simulador los contornos crecían
hasta 12 veces el papel.

## Versión web (Vercel)

La misma interfaz corre entera en el navegador, sin Python ni instalación.
El backend es un port a JavaScript de los servicios de Python (visión,
alineación, física y estado del juego) sobre OpenCV.js 4.12, la misma versión
de OpenCV que usa Python. Las pruebas de paridad exigen que el port dé las
mismas respuestas que Python en 2065 casos (`services/browser/README.md`).

- Abrí la página en Chrome, permití la cámara y seguí los mismos pasos que
  con el servidor local, alineación incluida.
- La primera carga descarga OpenCV (11 MB); después queda en caché.
- Diferencias: no hay control de la cámara por USB (solo se fija lo que
  Chrome permite), y los colores, los ajustes de cámara y los eventos quedan
  guardados en el navegador. `postitEvents()` en la consola lista los eventos.

Para construir y probar el sitio estático en la Mac:

```sh
npm ci
npm run build                     # dist/
node scripts/serve-static.mjs     # http://localhost:4870/
```

`vercel --prod` lo publica: `vercel.json` define el build y `.vercelignore`
sube solo los 21 archivos que el build necesita.

## Usar la cámara del celular

Un iPhone cerca de la Mac aparece como cámara (Cámara de continuidad), por
ejemplo "Cámara de iPhone de Tobias". Elegilo en el selector **Cámara**
debajo del video. El panel recuerda esa elección al recargar. Sin elección
guardada prefiere la C920, después el iPhone y recién después la cámara
FaceTime de la Mac, que mira al operador y no a la pared. El iPhone suele
aparecer unos segundos después de abrir la página. Si para entonces el panel
ya arrancó con la FaceTime, pasa solo al iPhone, siempre que todavía no
hayas elegido otra cámara a mano ni preparado un tablero. El tablero queda
asociado al nombre de la cámara: recargar la página con la misma cámara lo
conserva mientras llegan imágenes recientes, y elegir otra lo descarta porque cambia el encuadre. Si la cámara
no entrega imágenes en 8 segundos, el panel lo dice y muestra **Reintentar
cámara**, que vuelve a la cámara recordada.

- **Fijalo.** Usá un soporte firme para que el encuadre no cambie durante un
  intento. Si lo movés, empezá otro intento con la nueva detección.
- **Apagá los efectos.** En Centro de control → Efectos de video, apagá
  *Encuadre centrado*, que mueve el encuadre solo, y
  también *Retrato* y *Luz de estudio*.
- **Queda en automático.** El celular no es USB y Chrome no ofrece sus
  controles, así que el balance, la exposición y el foco no se pueden fijar.
  Para compensar, con una cámara automática el gotero espera 1 segundo con
  el proyector en negro antes de leer, en vez de 0.18 s, para que la
  exposición se acomode.
- **No toca otra cámara.** El ajuste fijo por USB solo se usa con una cámara
  cuyo nombre trae su ID USB. Con el celular activo, el panel nunca toca la
  C920 aunque esté enchufada.

## Fondo del proyector: oscuro o claro

Arriba a la derecha, junto a **Abrir proyección**, está **Fondo: Oscuro /
Claro**. También se cambia con la tecla `F`, desde el panel o desde la
ventana del proyector. El cambio se ve al instante en la ventana del
proyector y queda guardado en el navegador.

- **Oscuro**: fondo negro y pelota clara.
- **Claro**: fondo blanco suave (`#f3f2ec`) y pelota oscura.

En los dos fondos el proyector dibuja bordes neutros (blancos en el oscuro,
grafito en el claro) y nunca pone color sobre la pared vacía.

Al usar el gotero o buscar el ajuste de cámara, la ventana muestra solo su
fondo, sin bordes ni pelota. Así la lectura de colores y el ajuste de cámara
se hacen con la misma luz que habrá durante el juego, también con fondo claro.

## Colores de los post-its

Debajo del video hay una fila por tipo de post-it: inicio, llegada, rebotes y
esquinas. Cada una tiene:

- **Luz**: se enciende con el color elegido cuando la cámara ve lo que el
  juego necesita (1 inicio, 1 llegada; rebotes y esquinas opcionales).
  Queda en ámbar si ve una cantidad incorrecta y apagada si no ve
  ninguno. Al lado figura la cuenta, por ejemplo `4/4`.
- **Cuadrado de color**: abre el selector de color de Chrome. En macOS, ese
  selector trae su propia lupa para tomar un color de la pantalla.
- **Gotero**: pulsalo y tocá el centro del post-it en la imagen de la
  cámara. Deja la proyección un instante solo con su fondo (negro con el
  fondo oscuro), sin bordes ni pelota, para que la luz proyectada no tiña el
  papel. Lee un cuadrito de 11×11 píxeles y se queda
  con la parte más saturada: si el cuadrito pisa el borde, la pared no lava
  el color. `Esc` cancela.
- **Avisos**: si el color elegido es pálido, el panel lo dice. Con la imagen
  siguiente también avisa si algo empeoró, por ejemplo "Con #d2b996 no veo
  ningún post-it de rebotes", o si el color nuevo se confunde con otro tipo.

Sobre la imagen, cada post-it detectado se ilumina con un halo de su color.
Así se ve qué papel exacto cuenta como cada tipo. **Colores originales**
vuelve a los colores por defecto. Los colores quedan guardados en
`runs/palette.json` y se mantienen al reiniciar el servidor.

Límites:

- Dos tipos no pueden tener tonos casi iguales: tienen que estar a 16° o más
  en el círculo cromático.
- Se rechaza un color casi gris o muy oscuro, porque la pared o las sombras
  se confundirían con él.
- Un tono vecino a dos tipos se reparte por cercanía, así un papel nunca
  cuenta para dos.
- La luz proyectada altera los colores que ve la cámara. El gotero los lee
  con la proyección en su fondo neutro, la misma luz que habrá al jugar.
- Los colores se aplican a la siguiente detección en vivo. Durante un intento,
  `R` prepara una pelota nueva con la distribución actual.

## Cámara fija: nada en automático

El balance de blancos, la exposición y el foco automáticos cambian los colores
cuando cambia la luz del proyector. Por eso el panel los fija:

- **Al abrir la cámara**, si hay un ajuste guardado para esa cámara (por
  nombre, por ejemplo `HD Pro Webcam C920 (046d:082d)`), lo aplica en manual
  enseguida.
- **Si no hay ajuste guardado, lo busca solo y lo guarda.** La búsqueda nunca
  usa el automático: prueba valores fijos y elige el mejor. Tarda unos 15
  segundos.
- **Buscar ajuste fijo / Buscar otra vez** repite la búsqueda cuando quieras.
  Hacelo con la ventana de proyección abierta: la búsqueda la deja solo con
  su fondo y después vuelve a mostrar lo que había. Si el ajuste se
  buscó sin proyector, el estado lo avisa en ámbar.

La búsqueda prueba:

1. **Exposición y balance**: 5 exposiciones (5, 10, 20, 40 y 80 ms; casi
   todas múltiplos de 10 ms para no parpadear con la luz de 50 Hz) por 4
   balances (3000 a 6000 K).
2. **Foco**: 7 posiciones de foco con lo mejor del paso anterior.
3. **Refinamiento**: los vecinos del mejor ajuste.

Cada prueba la puntúa el backend con el mismo detector del escaneo, en este
orden de prioridad:

- que el escaneo funcione;
- cuántos tipos de post-it se ven bien;
- el color de los post-its;
- la pared expuesta cerca del 62 % de brillo, sin zonas quemadas ni negras.

El foco elige la imagen más nítida.

En la barra de la cámara se ve el resultado, por ejemplo
`Cámara fija: 4000 K · 20 ms · foco 35`.

**Por dónde fija la cámara.** Primero se lo pide a Chrome. Si Chrome no ofrece
modo manual, como pasa con la C920 en macOS, el servidor le habla a la cámara
directamente por USB (ver `services/uvc/README.md`), y el estado dice
`Cámara fija por USB: …`. En ese caso, cada 20 segundos el panel comprueba que
nada haya vuelto a automático (por ejemplo, si se desenchufó la cámara o la Mac
se durmió). Si pasó, vuelve a aplicar el ajuste y lo registra como `reapplied`.

Qué hace el panel según lo que se puede controlar de cada ajuste:

- **Modo manual y rango de valores**: puede apagar el automático, probar
  valores y guardar el mejor.
- **Solo modo manual**: apaga el automático y congela el valor que tenía la
  cámara. El estado dice "Cámara fija en los valores actuales".
- **Ningún modo manual**: no puede tocarlo. El estado dice "Chrome no ofrece
  modo manual…".

En cualquier caso, pasá el mouse sobre el estado para ver qué informó Chrome
de cada control. Si Chrome ofrece el manual pero un control no lo toma, el
estado lo marca ("no tomó el manual: foco").

Los ajustes quedan en `runs/camera-presets.json`. En `runs/web-events.jsonl`
quedan como `camera_report`:

- qué controles expone Chrome;
- qué valores quedaron realmente aplicados;
- el resultado de cada búsqueda.

Si cambiás la luz de la sala, buscá otra vez. Si cambia el color de la imagen,
volvé a tomar los colores con el gotero.

## Montaje

1. Usá una pared plana. Hacé que la webcam vea toda la zona de juego y que el
   proyector ilumine el mismo encuadre. Podés agregar post-its de esquina,
   pero el juego funciona también sin ellos.
2. Poné el inicio, la llegada y los rebotes dentro de esa zona. Dejá
   espacio entre papeles y una separación de al menos el radio de la pelota
   respecto de los bordes. Revisá que las luces de inicio y llegada estén
   encendidas antes de lanzar.
3. Fijá webcam y proyector. Usá iluminación ambiente estable y configurá el
   proyector como pantalla extendida de macOS. No hace falta que los
   encuadres coincidan: alcanza con que la cámara vea toda la proyección,
   porque la alineación mide la correspondencia.

## Modo clásico de pantalla completa

La aplicación original de Pygame sigue disponible. Desde esta carpeta:

```sh
./.venv/bin/python main.py
```

Por defecto se abre en la segunda pantalla (índice `1`) si existe y usa
la webcam `0`. En este Mac, AVFoundation enumera la cámara FaceTime como
`0` y la Logitech HD Pro Webcam C920 como `1`. Para usar la C920, abrí
**Terminal** y ejecutá:

```sh
cd /Users/tobiasarraiza/Documents/Codex/2026-10-02/bien/outputs/postit-wall-game
./.venv/bin/python main.py --camera 1
```

Cuando macOS solicite acceso a la cámara para Terminal, elegí **Permitir**.
Si no aparece el aviso, revisá Ajustes del Sistema → Privacidad y seguridad
→ Cámara y activá Terminal si figura allí. El permiso corresponde a la
aplicación que inicia Python.

## Calibración y partida en el modo clásico

El modo clásico usa siempre los colores por defecto: inicio amarillo, llegada
naranja, rebotes verdes y esquinas fucsia. Los colores elegidos en la
interfaz web no se aplican a este modo.

- `A` calibra automáticamente: el proyector muestra puntos blancos de
  coordenadas conocidas, la webcam los localiza y se calcula una homografía.
  Otros cuatro puntos verifican el error. Si la webcam no ve bastantes puntos,
  el programa ofrece calibración manual.
- `M` calibra manualmente: usá las flechas para mover una cruz hasta el centro
  de cada post-it fucsia, en el orden indicado. `Shift` reduce la velocidad y
  `Enter` confirma cada cruz.
- Antes de jugar, revisá que los contornos proyectados coincidan con los
  post-its y que la cuadrícula esté alineada. `Enter` acepta; `A` o `M` repite
  la calibración.
- `←` y `→` cambian la dirección de lanzamiento. `Espacio` lanza la pelota.
  La pelota rebota en los verdes, gana al tocar el naranja y pierde al salir
  del campo. `R` reinicia con el mismo tablero.
- Para **mover post-its entre intentos**, detené el intento y pulsá `S`. La
  proyección queda negra brevemente, se vuelve a leer el tablero y aparece una
  pelota nueva en el amarillo. `C` recalibra si se movió el equipo. `Esc`
  cierra el juego.

La calibración automática requiere que los puntos blancos proyectados sean
visibles para la webcam. Con luz muy intensa, sombras o exposición automática
variable, usá `M`. La calibración supone pared plana; una lente con mucha
distorsión puede necesitar corrección óptica antes de mapear puntos.

## Pruebas y evidencia

Las pruebas son sintéticas y se ejecutan sin hardware. Requieren Node.js
además de Python para validar la interfaz del navegador:

```sh
./scripts/verify.sh
```

El script ejecuta estas pruebas y evaluaciones (unos 2 minutos):

```sh
./.venv/bin/python -m unittest discover -s . -p 'test_*.py' -q
./.venv/bin/python -m services.calibration.evals.synthetic
./.venv/bin/python -m services.calibration.evals.coded
./.venv/bin/python -m services.vision.eval
./.venv/bin/python -m services.physics.evals.scenarios
./.venv/bin/python -m services.runtime.eval
./.venv/bin/python -m services.web.backend.eval
./.venv/bin/python -m tests.eval_web_flow
./.venv/bin/python -m services.web.contract.camera_trials --check
./.venv/bin/python -m services.uvc.smoke
./.venv/bin/python -m services.browser.parity.export
node --test services/browser/tests/*.test.mjs
node services/browser/evals/parity.mjs
node --test services/web/tests/*.test.mjs
node services/web/evals/flows.mjs
node services/web/evals/live-projection.mjs
./.venv/bin/python -m services.web.evals.eyedropper
./.venv/bin/python -m services.simulation.feedback_eval --jobs 4
```

Con `POSTIT_BROWSER=1 ./scripts/verify.sh` corre además en Chrome real
`tests/live-browser.mjs` y `tests/closed-loop-browser.mjs` (unos 4 minutos;
necesitan Chrome y Playwright).

`services/calibration/evals/coded.py` decodifica la alineación en 14
condiciones (exposición automática, reflejos, desenfoque, cuadros
mezclados): 140/140 sin ids equivocados, error contra la verdad p95 ≤ 0,11 px.

`services/simulation/feedback_eval.py` es el lazo cerrado: el backend real,
un proyector y una cámara simulados, y 18 escenarios (sin alinear, alineado
exacto o con 2 px de error, con y sin relleno, sala oscura, iluminada,
exposición automática y el sistema completo con su alineación y su chequeo
de color). Exige 0 parpadeos, 0 post-its fantasma, el borde a ≤ 4 px de
donde debe y nunca sobre el papel. La línea base con el proyector anterior
reproduce la falla medida en la pared real.

`services/browser/parity` exporta las respuestas de Python y los tests de
`services/browser` exigen que el port de JavaScript dé las mismas en 2065
casos: detección, refinamiento subpíxel, alineación con sus cinco motivos de
falla, sondas y guiones completos del controlador.

`tests/closed-loop-browser.mjs` abre el panel y el proyector reales en Chrome
con una cámara simulada que filma la pared iluminada por la ventana del
proyector, con los dos backends: la alineación sale a 0,03-0,08 px de la
verdad, los contornos a 0,4 px de los papeles y los bordes a 3 px por fuera.

`services/web/evals/eyedropper.py` mide el gotero: hace 588 clicks sobre
fotos de la pared en siete condiciones de luz y foco, y exige que todo click
adentro del post-it funcione y que ningún click fallido pase sin aviso.

`services/web/evals/flows.mjs` comprueba que cada botón del panel coincida
con lo que el backend acepta en cada fase del juego. Si cambiás reglas del
backend o la forma de `/api/state`, regenerá la matriz que usa:
`./.venv/bin/python -m services.web.contract.matrix`. Una prueba falla
mientras la matriz guardada no coincida con el backend.

`services/web/evals/live-projection.mjs` compara los contornos de todas las
muestras reales en pantallas horizontales, verticales y con distintas
proporciones. Exige 100 % de coincidencia de colores, geometría y pelota.
`tests/live-browser.mjs` verifica las dos ventanas con Chrome y una cámara
sintética. Ver `services/web/README.md` para ejecutarlo.

Lo que ninguna prueba sin hardware cubre: la lente, el enfoque y la
exposición automática de la cámara real, y el brillo del proyector real. La
alineación los mide en cada sesión y dice su error. Los eventos de la interfaz
web quedan en `runs/web-events.jsonl`; el modo clásico usa `runs/events.jsonl`.

## Estructura

- `contracts/`: datos y coordenadas compartidos.
- `services/vision/`: segmentación de los post-its por color.
- `services/calibration/`: puntos proyectados y homografía del modo clásico.
- `services/physics/`: pelota y colisiones en coordenadas del tablero.
- `services/runtime/`: captura de cámara, pantalla, controles y registro.
- `services/web/backend/`: servidor local, estado del juego y rutas HTTP.
- `services/web/ui/`: panel de control y ventana de proyección del navegador.
- `services/web/contract/`: matriz de estados del backend que leen las
  pruebas y la evaluación del navegador.
- `services/browser/`: port a JavaScript del backend para la versión web.
- `services/simulation/`: cámara, proyector y pared simulados; evaluación del
  lazo cerrado.
- `docs/alignment-and-color.md`: contrato de la alineación y de lo que
  proyecta cada modo.
- `services/uvc/`: control de la cámara por USB (balance, exposición y foco
  manuales) cuando Chrome no lo ofrece.
- `main.py`: entrada del programa.
- `web.py`: entrada de la interfaz web.

Cada servicio tiene sus propias pruebas, evaluación y explicación. El hook de
Git ejecuta las pruebas locales antes de cada commit; si clonás el proyecto,
activá el hook con `git config core.hooksPath .githooks`.
El hook divide la suite completa en grupos independientes que corren en
paralelo; mantiene la misma cobertura y limita los hilos de OpenCV.
