# Juego de pared con post-its

La webcam lee un tablero físico y el proyector dibuja una pelota encima. Hay
un post-it de inicio, uno de llegada, rebotes opcionales y **cuatro** de
esquina que marcan el campo. Por defecto, el inicio es amarillo, la llegada
naranja, los rebotes verdes y las esquinas fucsia o violetas. Desde la vista
de cámara podés elegir otros colores. Esta versión relee los post-its **entre
intentos**. La interfaz web muestra la cámara y los controles en la pantalla
principal; `J` abre otra ventana para proyectar sobre la pared.

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
Mantené esa pantalla en su tamaño definitivo antes de calibrar: un cambio de
tamaño invalida la alineación. Los controles de la partida permanecen en la
ventana principal. Si Chrome bloquea la ventana emergente, permití las
ventanas emergentes para `127.0.0.1` y volvé a pulsar `J`.

Los botones de escanear, calibrar y lanzar se habilitan recién con la cámara
y la ventana de proyección listas. Debajo de "Preparar la pared", el panel
muestra siempre el siguiente paso y resalta el botón que lo destraba.

Con la cámara y la proyección listas:

1. Pulsá `S` para escanear los post-its. La ventana del proyector se pone
   negra antes de tomar la imagen.
2. Pulsá `C` para calibrar. En la ventana del proyector, llevá la cruz al
   centro de cada post-it de esquina y confirmá las cuatro esquinas en el
   orden indicado. Revisá que los contornos proyectados coincidan con los
   papeles.
3. Ajustá la dirección desde el control principal y lanzá con `Espacio`.
   Tras ganar o perder, `R` inicia otro intento con el mismo tablero. Si
   moviste post-its, pulsá `S` para releerlos antes del siguiente intento.

Si se cierra la ventana del proyector durante un intento, la pelota queda en
pausa. Reabrila con `J` y pulsá **Continuar intento** (o `Espacio`). Con el
intento en pausa también podés releer los post-its, recalibrar o empezar otro
intento con `R`.

La vista de cámara sigue en directo durante la partida. El tablero del juego
se lee solo al escanear; las luces de detección en vivo son una guía previa.
Si movés la cámara, la pared o el proyector, repetí la calibración.

## Usar la cámara del celular

Un iPhone cerca de la Mac aparece como cámara (Cámara de continuidad), por
ejemplo "Cámara de iPhone de Tobias". Elegilo en el selector **Cámara**
debajo del video. El panel recuerda esa elección al recargar. Sin elección
guardada prefiere la C920, después el iPhone y recién después la cámara
FaceTime de la Mac, que mira al operador y no a la pared. El iPhone suele
aparecer unos segundos después de abrir la página. Si para entonces el panel
ya arrancó con la FaceTime, pasa solo al iPhone, siempre que todavía no
hayas escaneado ni elegido otra cámara a mano. El tablero escaneado y la calibración quedan
asociados al nombre de la cámara: recargar la página con la misma cámara los
conserva, y elegir otra los descarta porque cambia el encuadre. Si la cámara
no entrega imágenes en 8 segundos, el panel lo dice y muestra **Reintentar
cámara**, que vuelve a la cámara recordada.

- **Fijalo.** Usá un soporte firme y no lo muevas después de calibrar: si
  cambia el encuadre, hay que calibrar de nuevo.
- **Apagá los efectos.** En Centro de control → Efectos de video, apagá
  *Encuadre centrado*, que mueve el encuadre solo y rompe la calibración, y
  también *Retrato* y *Luz de estudio*.
- **Queda en automático.** El celular no es USB y Chrome no ofrece sus
  controles, así que el balance, la exposición y el foco no se pueden fijar.
  Para compensar, con una cámara automática el escaneo espera 1 segundo con
  el proyector en negro antes de capturar, en vez de 0.18 s, para que la
  exposición se acomode.
- **No toca otra cámara.** El ajuste fijo por USB solo se usa con una cámara
  cuyo nombre trae su ID USB. Con el celular activo, el panel nunca toca la
  C920 aunque esté enchufada.

## Fondo del proyector: oscuro o claro

Arriba a la derecha, junto a **Abrir proyección**, está **Fondo: Oscuro /
Claro**. También se cambia con la tecla `F`, desde el panel o desde la
ventana del proyector. El cambio se ve al instante en la ventana del
proyector y queda guardado en el navegador.

- **Oscuro**: fondo negro con trazos claros (el aspecto original).
- **Claro**: fondo blanco suave (`#f3f2ec`) con trazos oscuros: pelota y cruz
  casi negras, contornos verde azulado, dorado oscuro y óxido. Todos los
  trazos tienen contraste 4:1 o más contra el fondo; la cuadrícula de guía,
  2.5:1.

En los dos modos, al escanear o al buscar el ajuste de cámara, la ventana se
pone en **negro absoluto**. La lectura de colores y el ajuste fijo de cámara
siempre se hacen en las mismas condiciones. Con fondo claro, mientras jugás la
pared queda iluminada y la cámara la ve más clara: las luces de detección en
vivo pueden apagarse, pero el escaneo no se ve afectado.

## Colores de los post-its

Debajo del video hay una fila por tipo de post-it: inicio, llegada, rebotes y
esquinas. Cada una tiene:

- **Luz**: se enciende con el color elegido cuando la cámara ve lo que el
  escaneo necesita (1 inicio, 1 llegada, 4 esquinas; cualquier cantidad de
  rebotes). Queda en ámbar si ve una cantidad incorrecta y apagada si no ve
  ninguno. Al lado figura la cuenta, por ejemplo `4/4`.
- **Cuadrado de color**: abre el selector de color de Chrome. En macOS, ese
  selector trae su propia lupa para tomar un color de la pantalla.
- **Gotero**: pulsalo y tocá el centro del post-it en la imagen de la
  cámara. Apaga el proyector un instante, como el escaneo, para que la luz
  proyectada no tiña el papel. Lee un cuadrito de 11×11 píxeles y se queda
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
- Con la proyección del juego encendida, los contornos proyectados pueden
  iluminar papeles de más. El escaneo apaga la proyección antes de leer.
- Después de cambiar un color, escaneá otra vez para aplicarlo al tablero.

## Cámara fija: nada en automático

El balance de blancos, la exposición y el foco automáticos cambian los colores
cuando el proyector se apaga para escanear. Por eso el panel los fija:

- **Al abrir la cámara**, si hay un ajuste guardado para esa cámara (por
  nombre, por ejemplo `HD Pro Webcam C920 (046d:082d)`), lo aplica en manual
  enseguida.
- **Si no hay ajuste guardado, lo busca solo y lo guarda.** La búsqueda nunca
  usa el automático: prueba valores fijos y elige el mejor. Tarda unos 15
  segundos.
- **Buscar ajuste fijo / Buscar otra vez** repite la búsqueda cuando quieras.
  Hacelo con la ventana de proyección abierta: la búsqueda la pone en negro
  como al escanear y después vuelve a mostrar lo que había. Si el ajuste se
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

1. Usá una pared plana. Colocá los cuatro post-its de esquina cerca de las
   cuatro esquinas del área proyectada. La webcam debe verlos a todos y el
   proyector debe iluminar todo el campo.
2. Poné el inicio, la llegada y los rebotes dentro de ese rectángulo. Dejá
   espacio entre papeles y una separación de al menos el radio de la pelota
   respecto de los bordes. Revisá que las cuatro luces debajo del video estén
   encendidas antes de escanear.
3. Fijá webcam y proyector. Usá iluminación ambiente estable y configurá el
   proyector como pantalla extendida de macOS. Si se mueve cualquiera de los
   dos, pulsá `C` para recalibrar.

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

El script ejecuta estas pruebas y evaluaciones:

```sh
./.venv/bin/python -m unittest discover -s . -p 'test_*.py' -q
./.venv/bin/python -m services.calibration.evals.synthetic
./.venv/bin/python -m services.vision.eval
./.venv/bin/python -m services.physics.evals.scenarios
./.venv/bin/python -m services.runtime.eval
./.venv/bin/python -m services.web.backend.eval
./.venv/bin/python -m tests.eval_web_flow
./.venv/bin/python -m services.web.contract.camera_trials --check
./.venv/bin/python -m services.uvc.smoke
node --test services/web/tests/*.test.mjs
node services/web/evals/flows.mjs
./.venv/bin/python -m services.web.evals.eyedropper
```

`services/web/evals/eyedropper.py` mide el gotero: hace 588 clicks sobre
fotos de la pared en siete condiciones de luz y foco, y exige que todo click
adentro del post-it funcione y que ningún click fallido pase sin aviso.

`services/web/evals/flows.mjs` comprueba que cada botón del panel coincida
con lo que el backend acepta en cada fase del juego. Si cambiás reglas del
backend o la forma de `/api/state`, regenerá la matriz que usa:
`./.venv/bin/python -m services.web.contract.matrix`. Una prueba falla
mientras la matriz guardada no coincida con el backend.

La prueba física que falta hacer en la pared es revisar la cuadrícula sobre
papeles reales y confirmar que el error máximo mostrado en la calibración
  automática sea menor que la mitad del radio de la pelota. Cada calibración, lectura de
tablero e intento registra valores en `runs/events.jsonl` para poder revisar
errores de alineación, cantidad de rebotes y tiempo de llegada.

## Estructura

- `contracts/`: datos y coordenadas compartidos.
- `services/vision/`: segmentación de los post-its con el proyector en negro.
- `services/calibration/`: puntos proyectados y homografía cámara → proyector.
- `services/physics/`: pelota y colisiones en píxeles del proyector.
- `services/runtime/`: captura de cámara, pantalla, controles y registro.
- `services/web/backend/`: servidor local, estado del juego y rutas HTTP.
- `services/web/ui/`: panel de control y ventana de proyección del navegador.
- `services/web/contract/`: matriz de estados del backend que leen las
  pruebas y la evaluación del navegador.
- `services/uvc/`: control de la cámara por USB (balance, exposición y foco
  manuales) cuando Chrome no lo ofrece.
- `main.py`: entrada del programa.
- `web.py`: entrada de la interfaz web.

Cada servicio tiene sus propias pruebas, evaluación y explicación. El hook de
Git ejecuta las pruebas locales antes de cada commit; si clonás el proyecto,
activá el hook con `git config core.hooksPath .githooks`.
